"""
``ExportSpec``: modelo, resolución de selección y validación — módulo **PURO**.

Sin motor, sin sesión, sin FastAPI: igual que ``schema_diff``, ``plan_integrity`` y
``query_policy``, este módulo se puede ejercitar entero sin una base de datos delante.
Ahí está su valor: es la pieza que decide **qué se va a exportar y con qué opciones**, y
esa decisión tiene que ser reproducible y testeable antes de que exista una conexión.

Contiene cuatro responsabilidades, todas derivadas del diseño
``docs/plans/10-exportacion-de-bases-de-datos.md``:

1. **El vocabulario** (§4). Los enumerados son el tipo, no una validación dispersa. El
   caso testigo es ``scope_ddl``/``entity_ddl``: con dos booleanos
   (``drop`` + ``create``) el estado "eliminar sin crear" **es representable** y tarde o
   temprano se cuela por la API; con un enumerado de cuatro valores sencillamente no
   existe. Ver §4.1.
2. **La selección** (§5.1). Tres modos más patrones glob evaluados **contra los nombres
   que devolvió el catálogo del motor**, jamás inyectados en SQL, con las tablas internas
   del gateway (``_gw_v_``/``_gw_stg_``) descartadas siempre — es el fix del incidente de
   producción de 2026-07-27.
3. **La matriz de compatibilidad** (§11.1). Se **publica** en el endpoint de capacidades
   y se **hace cumplir** en el servidor con la MISMA estructura de datos: publicarla sin
   validarla sería peor que no publicarla, y validar con un criterio distinto del que se
   publica es cómo el cliente termina adivinando.
4. **La validación del filtro de filas** (§9.2), que es el único punto donde entra SQL
   escrito por una persona. Se valida con la maquinaria que ya existe (``query_policy``),
   no con una segunda.

Los códigos de error son estables y viajan en ``public_context`` (§11.2), **no** en
``context``, que solo se ve en ``development``: en producción el operador recibiría "hay
una opción inválida" sin saber cuál. Es el mismo error que ya se corrigió en
``apply-profile``.
"""

from __future__ import annotations

import enum
import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

import sqlglot
from sqlglot import exp

from app.exceptions import AppHttpException
from app.services.db_admin import query_policy
from app.services.db_admin.identifiers import (
    is_gateway_internal_table,
    quote_identifier,
)
from app.services.db_admin.sql_literals import BINARY_ENCODINGS

# --------------------------------------------------------------------------- #
# Códigos de error estables (§11.2)                                            #
# --------------------------------------------------------------------------- #
# Van en ``public_context``, no en ``context``. El cliente los usa para decidir qué
# pantalla mostrar; cambiarlos rompe al frontend, así que se tratan como contrato.

CODE_INCOMPATIBLE_OPTION = "export.incompatible_option"
CODE_DATA_WITHOUT_STRUCTURE = "export.data_without_structure"
CODE_MISSING_DEPENDENCIES = "export.missing_dependencies"
CODE_INVALID_ROW_FILTER = "export.invalid_row_filter"
CODE_INLINE_TOO_LARGE = "export.inline_too_large"
CODE_FINGERPRINT_CHANGED = "export.fingerprint_changed"
CODE_ARTIFACT_EXPIRED = "export.artifact_expired"
CODE_ARTIFACT_CONSUMED = "export.artifact_consumed"
CODE_QUOTA_EXCEEDED = "export.quota_exceeded"
# El destino elegido no se puede exportar por lo que ES, no por cómo se pidió: hoy, la
# propia base de metadatos del gateway.
CODE_SCOPE_NOT_ALLOWED = "export.scope_not_allowed"

ERROR_CODES: frozenset[str] = frozenset(
    {
        CODE_SCOPE_NOT_ALLOWED,
        CODE_INCOMPATIBLE_OPTION,
        CODE_DATA_WITHOUT_STRUCTURE,
        CODE_MISSING_DEPENDENCIES,
        CODE_INVALID_ROW_FILTER,
        CODE_INLINE_TOO_LARGE,
        CODE_FINGERPRINT_CHANGED,
        CODE_ARTIFACT_EXPIRED,
        CODE_ARTIFACT_CONSUMED,
        CODE_QUOTA_EXCEEDED,
    }
)


# --------------------------------------------------------------------------- #
# Enumerados (§4)                                                              #
# --------------------------------------------------------------------------- #
# Se usa ``enum.StrEnum`` (3.11+) y no el ``(str, enum.Enum)`` de ``app/models/enums.py``
# por una razón concreta: con el mixin clásico ``str(Miembro)`` devuelve
# ``"ScopeDdl.NONE"``, no ``"NONE"``. Esa diferencia es invisible hasta que un valor se
# interpola en un mensaje, en una clave de caché o en el hash del ``confirm_token`` — y
# acá los valores se comparan y se serializan constantemente. Estos enums no son de ORM,
# así que no hay motivo para arrastrar el footgun.


class ScopeDdl(enum.StrEnum):
    """
    Qué DDL se emite para el CONTENEDOR (la base de datos / el esquema).

    Los cuatro valores, y por qué (§4.1):

    - ``NONE``: no se toca el contenedor (el destino ya existe).
    - ``CREATE``: crear; falla si ya existe.
    - ``DROP_CREATE``: destruir y recrear — "quiero que quede exactamente esto".
    - ``CREATE_IF_NOT_EXISTS``: "quiero que exista, sin tocar lo que ya está".

    Las dos últimas NO son redundantes: son las dos idempotencias que la gente quiere y
    son incompatibles entre sí. Ofrecer solo una obliga a editar el script a mano.

    **El estado "eliminar sin crear" no es representable**: no hay valor para él. Esa es
    la razón de que sea un enumerado y no dos banderas.
    """

    NONE = "NONE"
    CREATE = "CREATE"
    DROP_CREATE = "DROP_CREATE"
    CREATE_IF_NOT_EXISTS = "CREATE_IF_NOT_EXISTS"


class EntityDdl(enum.StrEnum):
    """Ídem ``ScopeDdl`` pero para cada OBJETO (tabla, vista, rutina…)."""

    NONE = "NONE"
    CREATE = "CREATE"
    DROP_CREATE = "DROP_CREATE"
    CREATE_IF_NOT_EXISTS = "CREATE_IF_NOT_EXISTS"


class Format(enum.StrEnum):
    """
    Formato del artefacto.

    Solo ``sql`` transporta estructura EJECUTABLE; en los demás la estructura existe a lo
    sumo como metadato del manifiesto, y por eso son incompatibles con toda opción de DDL
    (ver la matriz).
    """

    sql = "sql"
    csv = "csv"
    json = "json"
    ndjson = "ndjson"


class InsertVariant(enum.StrEnum):
    """Forma de la sentencia de datos en el artefacto ``sql``."""

    none = "none"
    insert = "insert"
    insert_ignore = "insert_ignore"
    replace = "replace"
    upsert = "upsert"


class DefinerMode(enum.StrEnum):
    """
    Tratamiento de la cláusula ``DEFINER`` de rutinas/vistas/triggers/eventos.

    Es un concepto de la familia MySQL. PostgreSQL no lo tiene (la propiedad del objeto
    ``ALTER … OWNER TO`` y ``SECURITY DEFINER`` son mecanismos distintos), así que allá
    ``keep`` es no-op y ``omit``/``replace`` se rechazan (§7.1).

    De ahí ``auto``, que es el DEFAULT: "el servidor elige el valor aplicable a este motor"
    (``omit`` en MySQL/MariaDB, ``keep`` en PostgreSQL). Sin él, el default global tenía que
    ser un valor concreto y cualquiera de los dos rompía a un motor: con ``omit``, un
    ``POST`` con cuerpo ``{}`` —la llamada canónica "exportá esta base"— devolvía **422**
    contra PostgreSQL porque la matriz lo prohíbe allí. Un ``omit`` **explícito** contra
    PostgreSQL sigue siendo 422: lo que ``auto`` arregla es el default, no la regla.

    Capabilities publica el default YA RESUELTO para el motor consultado, así que lo que el
    cliente ve y lo que el servidor aplica siguen saliendo de la misma fuente.
    """

    keep = "keep"
    omit = "omit"
    replace = "replace"
    auto = "auto"


class AutoincrementMode(enum.StrEnum):
    """
    Qué hacer con el contador de autoincremento.

    ``auto`` = conservarlo solo si esa tabla se exporta CON datos; sin datos, un
    ``AUTO_INCREMENT=5000`` en la definición es basura que confunde al que lea el script.
    """

    keep = "keep"
    omit = "omit"
    auto = "auto"


class ConstraintsPlacement(enum.StrEnum):
    """
    ``inline`` = índices/FKs dentro del ``CREATE TABLE``; ``deferred`` = al final, después
    de los datos (mucho más rápido de cargar y sin problemas de orden entre tablas).
    """

    inline = "inline"
    deferred = "deferred"


class Organization(enum.StrEnum):
    """``single`` = un artefacto; ``per_object`` = un archivo por objeto (§15, punto 3)."""

    single = "single"
    per_object = "per_object"


class Compression(enum.StrEnum):
    """``gzip`` comprime UN flujo; ``zip`` es un CONTENEDOR (el único apto multiarchivo)."""

    none = "none"
    gzip = "gzip"
    zip = "zip"


class Delivery(enum.StrEnum):
    """``file`` = descarga; ``inline`` = texto plano acotado para copiar al portapapeles."""

    file = "file"
    inline = "inline"


class SelectionMode(enum.StrEnum):
    """
    Modo de selección de la ESTRUCTURA.

    ``all_except`` existe porque es el flujo real ("marco todo y quito tres") y expresarlo
    con ``include`` obliga al cliente a enumerar el catálogo entero — lista que además
    envejece en cuanto alguien crea una tabla.
    """

    all = "all"
    include = "include"
    all_except = "all_except"


class DataSelectionMode(enum.StrEnum):
    """
    Modo de selección de los DATOS: los mismos tres, más ``none``.

    Es un enumerado APARTE y no ``SelectionMode`` con un valor extra para que "no exportar
    estructura" no sea representable: una exportación sin ningún objeto de estructura no
    es una opción del usuario, es una selección vacía.
    """

    none = "none"
    all = "all"
    include = "include"
    all_except = "all_except"


class OnError(enum.StrEnum):
    """``stop`` corta al primer fallo; ``continue`` es best-effort y reporta (§14)."""

    stop = "stop"
    continue_ = "continue"


class BinaryEncoding(enum.StrEnum):
    """Codificación de los binarios en los formatos de TEXTO (§8.2)."""

    hex = "hex"
    base64 = "base64"


class CharsetOverrideMode(enum.StrEnum):
    """``keep`` = el del origen; ``override`` = el par charset/collation indicado."""

    keep = "keep"
    override = "override"


class LineTerminator(enum.StrEnum):
    """
    Terminador de línea del formato delimitado, **por nombre** y no por el carácter.

    Se publica ``lf``/``crlf`` en vez de ``"\\n"``/``"\\r\\n"`` porque un control de
    formulario con dos opciones nombradas no puede mandar un terminador a medias, y porque
    un valor de un solo carácter invisible viaja mal por JSON, por un log y por un mensaje
    de error. ``.text`` devuelve el carácter real.

    Solo hay dos valores y es deliberado: ``\\r`` a secas (Mac clásico) no lo entiende
    ningún importador vivo, y admitir un terminador arbitrario convertiría el escapado en
    un problema abierto —habría que cuotear todo campo que contuviera ese texto—. Con dos
    valores la regla es fija: se cuotea si el campo trae ``\\r`` o ``\\n``.
    """

    lf = "lf"
    crlf = "crlf"

    @property
    def text(self) -> str:
        return "\r\n" if self is LineTerminator.crlf else "\n"


DEFAULT_FILENAME_TEMPLATE = "{database}-{date}-{job_id}"

# Tokens admitidos en ``output.filename_template`` (§9.2). Whitelist CERRADA: cualquier
# otro token es un 422, nunca una sustitución vacía silenciosa.
FILENAME_TOKENS: tuple[str, ...] = ("database", "object", "date", "time", "job_id")

# Tope de longitud del nombre de archivo final. El límite real del sistema de archivos es
# 255 bytes; se corta bastante antes para dejar lugar a los sufijos que agrega la entrega
# (``.part07.sql.gz``).
MAX_FILENAME_CHARS = 120

# Tope de longitud del filtro de filas. No es una regla de negocio: acota el costo de
# parsear texto arbitrario con sqlglot antes de haber validado nada.
MAX_ROW_FILTER_CHARS = 4000


# --------------------------------------------------------------------------- #
# Estructuras del spec (§4)                                                    #
# --------------------------------------------------------------------------- #
# Dataclasses frozen y no modelos Pydantic: este módulo es PURO y lo consumen tanto la
# capa de API (que traduce su schema Pydantic a esto) como los tests, que necesitan armar
# un spec en una línea sin levantar FastAPI.


@dataclass(frozen=True)
class CatalogObject:
    """
    Un objeto tal como lo devolvió el catálogo del motor.

    ``name`` es el nombre REAL del motor: es la única fuente admitida de identificadores
    para el artefacto. Nada que venga del cliente se usa como nombre de objeto.
    """

    object_type: str
    name: str


@dataclass(frozen=True)
class Selection:
    """Una selección declarativa (sin resolver) sobre el catálogo."""

    mode: SelectionMode | DataSelectionMode = SelectionMode.all
    types: tuple[str, ...] = ()  # vacío => todos los tipos presentes en el catálogo
    names: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSelection:
    """
    Selección ya resuelta a una lista EXPLÍCITA de objetos (§5.2).

    Es lo que se congela en el ``preview`` y lo que hashea el ``confirm_token``: un objeto
    creado entre el preview y el execute no entra, y si el catálogo cambió el fingerprint
    no coincide y hay que volver a previsualizar.
    """

    objects: tuple[CatalogObject, ...] = ()
    # Tablas de contabilidad interna del gateway descartadas del catálogo. Se informan
    # para que nadie las busque en el artefacto y crea que se perdieron.
    excluded_internal: tuple[str, ...] = ()
    # Nombres pedidos explícitamente que el catálogo no tiene. El controller decide si es
    # un 422 (selección explícita) o un aviso; acá solo se reportan.
    unknown_names: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(o.name for o in self.objects)

    @property
    def keys(self) -> frozenset[tuple[str, str]]:
        """``(object_type, name)`` — la identidad con la que se compara un objeto."""
        return frozenset((o.object_type, o.name) for o in self.objects)


@dataclass(frozen=True)
class RowFilter:
    """Filtro por objeto: ``where`` arbitrario (validado, §9.2) y tope de filas."""

    where: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class StructureOptions:
    """Opciones de DDL. ``drop_if_exists``/``drop_cascade`` modulan el DROP_CREATE."""

    scope_ddl: ScopeDdl = ScopeDdl.NONE
    entity_ddl: EntityDdl = EntityDdl.CREATE
    drop_if_exists: bool = True
    drop_cascade: bool = False
    # Nombre del contenedor re-tecleado por el operador. Este módulo solo exige que esté
    # presente; compararlo con el nombre REAL es del controller (patrón
    # ``confirm_target_name``), que es quien lo conoce.
    confirm_scope_drop: str | None = None


@dataclass(frozen=True)
class DataOptions:
    """Selección de datos (⊆ estructura, §5.3) + cómo se rendean las filas."""

    mode: DataSelectionMode = DataSelectionMode.none
    names: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    insert_variant: InsertVariant = InsertVariant.insert
    rows_per_statement: int = 200
    max_statement_bytes: int = 1048576
    include_column_list: bool = True
    per_object: Mapping[str, RowFilter] = field(default_factory=dict)

    @property
    def selection(self) -> Selection:
        """
        Vista de estas opciones como ``Selection`` para ``resolve_selection``.

        Los datos solo salen de TABLAS, así que el tipo va fijo: pedir "los datos de una
        vista" no es una opción que el usuario deba poder expresar.
        """
        return Selection(
            mode=self.mode,
            types=("table",),
            names=tuple(self.names),
            include_patterns=tuple(self.include_patterns),
            exclude_patterns=tuple(self.exclude_patterns),
        )


@dataclass(frozen=True)
class CharsetOverride:
    mode: CharsetOverrideMode = CharsetOverrideMode.keep
    charset: str | None = None
    collation: str | None = None


@dataclass(frozen=True)
class SanitizeOptions:
    """
    Qué se limpia del DDL emitido.

    ``script_comments`` y ``object_comments`` son opciones SEPARADAS a propósito: la
    primera son los comentarios DEL SCRIPT (encabezado, separadores), la segunda los
    ``COMMENT`` del ESQUEMA, que son parte de la definición y perderlos es una pérdida de
    información real.
    """

    script_comments: bool = True
    object_comments: bool = True
    definer: DefinerMode = DefinerMode.auto
    definer_value: str | None = None
    autoincrement: AutoincrementMode = AutoincrementMode.auto
    engine_specific_options: bool = False
    partitions: bool = True
    constraints_placement: ConstraintsPlacement = ConstraintsPlacement.deferred
    session_preamble: bool = True
    transaction_wrap: bool = False
    charset_override: CharsetOverride = field(default_factory=CharsetOverride)


@dataclass(frozen=True)
class CsvOptions:
    """
    Dialecto del formato delimitado (§8.1 del prompt). Solo aplica a ``format='csv'``.

    **La distinción entre NULL y cadena vacía se resuelve acá**, y es la razón de que
    ``null_representation`` exista. ``sql_literals.render_value_text`` devuelve ``""`` para
    ``None`` y declara que distinguirlos es responsabilidad del llamador; en un CSV eso
    importa de verdad (una columna ``NOT NULL`` con ``''`` y una nullable con ``NULL`` no
    son el mismo dato, y al reimportar la diferencia se pierde para siempre).

    El criterio, que es el de ``COPY … WITH CSV`` de PostgreSQL:

    - ``NULL`` se escribe **sin comillas** como ``null_representation`` (por defecto, el
      campo vacío);
    - la cadena vacía se escribe **siempre entre comillas** (``""``);
    - un valor de texto que coincide LITERALMENTE con ``null_representation`` también se
      cuotea, para que no se lea como nulo al reimportar.

    Ese es el único juego de reglas en el que ``NULL``, ``''`` y ``'\\N'`` son tres cosas
    distinguibles en el archivo, y por eso no es configurable.
    """

    delimiter: str = ","
    quote_char: str = '"'
    # ``None`` ⇒ RFC 4180: la comilla se duplica. Un carácter ⇒ estilo MySQL/Unix.
    escape_char: str | None = None
    line_terminator: LineTerminator = LineTerminator.lf
    header: bool = True
    null_representation: str = ""
    # Marca de orden de bytes al principio de cada archivo (y de cada fragmento). Existe
    # por Excel, que sin ella lee un UTF-8 con acentos como si fuera Latin-1.
    bom: bool = False


@dataclass(frozen=True)
class OutputOptions:
    organization: Organization = Organization.single
    split_max_bytes: int | None = None
    compression: Compression = Compression.none
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    file_encoding: str = "utf-8"
    delivery: Delivery = Delivery.file
    binary_encoding: BinaryEncoding = BinaryEncoding.hex
    # Manifiesto DESCRIPTIVO del esquema junto a los datos (solo json/ndjson). Es
    # documentación legible por máquina —columnas, tipos, claves, índices, relaciones—,
    # **nunca un script ejecutable**: desde ahí no se restaura nada. Ver §11.1 y el campo
    # ``executable: false`` que lleva el propio manifiesto.
    schema_manifest: bool = False


@dataclass(frozen=True)
class ExportSpec:
    """
    La petición completa, serializable y **autosuficiente**: basta por sí sola para
    reproducir el mismo artefacto (§3). Se persiste íntegra en ``export_jobs.spec``.
    """

    format: Format = Format.sql
    structure: StructureOptions = field(default_factory=StructureOptions)
    selection: Selection = field(default_factory=Selection)
    data: DataOptions = field(default_factory=DataOptions)
    sanitize: SanitizeOptions = field(default_factory=SanitizeOptions)
    output: OutputOptions = field(default_factory=OutputOptions)
    # Dialecto del formato delimitado. Bloque APARTE de ``output`` y no dentro de él porque
    # solo tiene sentido para un formato: mezclarlo con las opciones de entrega (que aplican
    # a los cuatro) haría creer que ``delimiter`` significa algo en un artefacto ``sql``.
    csv: CsvOptions = field(default_factory=CsvOptions)
    on_error: OnError = OnError.continue_
    idempotency_key: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> ExportSpec:
        """
        Construye un ``ExportSpec`` desde el JSON persistido o el cuerpo de la petición.

        Existe para que el spec guardado en ``export_jobs.spec`` se pueda revivir tal cual
        (reproducibilidad, §3) sin que la capa de API sea la única que sabe armarlo. Un
        valor fuera del enumerado es un ``422`` con el código estable, no un
        ``ValueError`` genérico: en el borde HTTP Pydantic lo atrapa antes, pero un spec
        persistido con una versión anterior del gateway también pasa por acá.
        """
        data = dict(payload or {})
        struct = dict(data.get("structure") or {})
        sel = dict(data.get("selection") or {})
        dat = dict(data.get("data") or {})
        san = dict(data.get("sanitize") or {})
        out = dict(data.get("output") or {})
        csv_opts = dict(data.get("csv") or {})
        chs = dict(san.get("charset_override") or {})
        per_object = {
            str(name): RowFilter(
                where=(spec or {}).get("where"),
                limit=(spec or {}).get("limit"),
            )
            for name, spec in (dat.get("per_object") or {}).items()
        }
        return cls(
            format=_enum(Format, data.get("format"), "format"),
            structure=StructureOptions(
                scope_ddl=_enum(
                    ScopeDdl, struct.get("scope_ddl"), "structure.scope_ddl"
                ),
                entity_ddl=_enum(
                    EntityDdl, struct.get("entity_ddl"), "structure.entity_ddl"
                ),
                drop_if_exists=_bool(struct.get("drop_if_exists"), True),
                drop_cascade=_bool(struct.get("drop_cascade"), False),
                confirm_scope_drop=struct.get("confirm_scope_drop"),
            ),
            selection=Selection(
                mode=_enum(SelectionMode, sel.get("mode"), "selection.mode"),
                types=_strs(sel.get("types")),
                names=_strs(sel.get("names")),
                include_patterns=_strs(sel.get("include_patterns")),
                exclude_patterns=_strs(sel.get("exclude_patterns")),
            ),
            data=DataOptions(
                mode=_enum(DataSelectionMode, dat.get("mode"), "data.mode"),
                names=_strs(dat.get("names")),
                include_patterns=_strs(dat.get("include_patterns")),
                exclude_patterns=_strs(dat.get("exclude_patterns")),
                insert_variant=_enum(
                    InsertVariant, dat.get("insert_variant"), "data.insert_variant"
                ),
                rows_per_statement=int(dat.get("rows_per_statement") or 200),
                max_statement_bytes=int(dat.get("max_statement_bytes") or 1048576),
                include_column_list=_bool(dat.get("include_column_list"), True),
                per_object=per_object,
            ),
            sanitize=SanitizeOptions(
                script_comments=_bool(san.get("script_comments"), True),
                object_comments=_bool(san.get("object_comments"), True),
                definer=_enum(DefinerMode, san.get("definer"), "sanitize.definer"),
                definer_value=san.get("definer_value"),
                autoincrement=_enum(
                    AutoincrementMode, san.get("autoincrement"), "sanitize.autoincrement"
                ),
                engine_specific_options=_bool(san.get("engine_specific_options"), False),
                partitions=_bool(san.get("partitions"), True),
                constraints_placement=_enum(
                    ConstraintsPlacement,
                    san.get("constraints_placement"),
                    "sanitize.constraints_placement",
                ),
                session_preamble=_bool(san.get("session_preamble"), True),
                transaction_wrap=_bool(san.get("transaction_wrap"), False),
                charset_override=CharsetOverride(
                    mode=_enum(
                        CharsetOverrideMode,
                        chs.get("mode"),
                        "sanitize.charset_override.mode",
                    ),
                    charset=chs.get("charset"),
                    collation=chs.get("collation"),
                ),
            ),
            output=OutputOptions(
                organization=_enum(
                    Organization, out.get("organization"), "output.organization"
                ),
                split_max_bytes=(
                    int(out["split_max_bytes"])
                    if out.get("split_max_bytes") is not None
                    else None
                ),
                compression=_enum(
                    Compression, out.get("compression"), "output.compression"
                ),
                filename_template=str(
                    out.get("filename_template") or DEFAULT_FILENAME_TEMPLATE
                ),
                file_encoding=str(out.get("file_encoding") or "utf-8"),
                delivery=_enum(Delivery, out.get("delivery"), "output.delivery"),
                binary_encoding=_enum(
                    BinaryEncoding, out.get("binary_encoding"), "output.binary_encoding"
                ),
                schema_manifest=_bool(out.get("schema_manifest"), False),
            ),
            csv=CsvOptions(
                # Los caracteres del dialecto NO caen a un default cuando llegan vacíos: un
                # ``delimiter: ""`` es un error del cliente y tiene que llegar a la
                # validación para que responda cuál es la opción culpable, no convertirse en
                # una coma en silencio. Solo ``None`` (ausente) toma el default.
                delimiter=_char(csv_opts.get("delimiter"), ","),
                quote_char=_char(csv_opts.get("quote_char"), '"'),
                escape_char=(
                    None
                    if csv_opts.get("escape_char") is None
                    else str(csv_opts["escape_char"])
                ),
                line_terminator=_enum(
                    LineTerminator, csv_opts.get("line_terminator"), "csv.line_terminator"
                ),
                header=_bool(csv_opts.get("header"), True),
                null_representation=(
                    ""
                    if csv_opts.get("null_representation") is None
                    else str(csv_opts["null_representation"])
                ),
                bom=_bool(csv_opts.get("bom"), False),
            ),
            on_error=_enum(OnError, data.get("on_error"), "on_error"),
            idempotency_key=data.get("idempotency_key"),
        )


def _strs(value) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(v) for v in value)


def _bool(value, default: bool) -> bool:
    return default if value is None else bool(value)


def _char(value, default: str) -> str:
    """Ausente ⇒ default; presente ⇒ tal cual (aunque sea inválido: lo dirá la matriz)."""
    return default if value is None else str(value)


def _enum(cls, value, field_path: str):
    """Valor -> miembro del enumerado, o 422 con el código estable si no pertenece."""
    if value is None:
        # ``None`` significa "no lo mandaron": vale el default declarado para esa ruta.
        return _DEFAULTS[field_path]
    try:
        return cls(str(value))
    except ValueError:
        raise AppHttpException(
            message=f"El valor de '{field_path}' no es una opción válida.",
            status_code=422,
            public_context={
                "code": CODE_INCOMPATIBLE_OPTION,
                "field": field_path,
                "allowed": [m.value for m in cls],
            },
        ) from None


# Defaults por RUTA, para que ``from_dict`` y las dataclasses no puedan divergir: si un
# default cambia en la dataclass y no acá (o al revés), el test de coherencia falla.
_DEFAULTS: dict[str, Any] = {
    "format": Format.sql,
    "structure.scope_ddl": ScopeDdl.NONE,
    "structure.entity_ddl": EntityDdl.CREATE,
    "selection.mode": SelectionMode.all,
    "data.mode": DataSelectionMode.none,
    "data.insert_variant": InsertVariant.insert,
    "sanitize.definer": DefinerMode.auto,
    "sanitize.autoincrement": AutoincrementMode.auto,
    "sanitize.constraints_placement": ConstraintsPlacement.deferred,
    "sanitize.charset_override.mode": CharsetOverrideMode.keep,
    "output.organization": Organization.single,
    "output.compression": Compression.none,
    "output.delivery": Delivery.file,
    "output.binary_encoding": BinaryEncoding.hex,
    "csv.line_terminator": LineTerminator.lf,
    "on_error": OnError.continue_,
}


# --------------------------------------------------------------------------- #
# Selección (§5.1)                                                             #
# --------------------------------------------------------------------------- #


def resolve_selection(
    catalog: Iterable[CatalogObject], selection: Selection
) -> ResolvedSelection:
    """
    Resuelve una selección declarativa a la lista EXPLÍCITA de objetos, en el orden EXACTO
    del §5.1:

    ```
    candidatos = catálogo(tipos)  −  tablas internas del gateway
    mode=none        → vacío
    mode=include     → candidatos ∩ names
    mode=all_except  → candidatos − names
    include_patterns → quedarse con los que matchean alguno
    exclude_patterns → quitar los que matchean alguno      (la exclusión GANA)
    ```

    Tres decisiones que importan:

    - **Los patrones son ``fnmatch`` contra nombres del CATÁLOGO, nunca SQL.** No hay
      ninguna ruta por la que un patrón llegue al motor: es filtrado en memoria sobre
      cadenas que el propio motor devolvió.
    - Se usa ``fnmatchcase`` y no ``fnmatch``: este último aplica ``os.path.normcase``,
      que en Windows pasa todo a minúsculas. Un mismo spec daría selecciones distintas
      según el sistema operativo del gateway, lo que rompe la reproducibilidad de §3.
    - **El orden del catálogo se preserva.** Es el orden que el adapter garantiza y del
      que depende el determinismo byte a byte de §8.3; reordenar acá (aunque fuera
      alfabéticamente) rompería la comparación de dos volcados.
    """
    typed = [
        o
        for o in catalog
        if not selection.types or o.object_type in tuple(selection.types)
    ]
    catalog_names = {o.name for o in typed}

    internal = tuple(o.name for o in typed if is_gateway_internal_table(o.name))
    candidates = [o for o in typed if not is_gateway_internal_table(o.name)]

    mode = str(selection.mode)
    wanted = tuple(selection.names)
    # Los nombres pedidos que el catálogo no tiene se reportan SIEMPRE, también en
    # ``all_except``: pedir la exclusión de una tabla que ya no existe suele ser un spec
    # viejo, y descubrirlo tarde (con la tabla real ya exportada) es peor.
    unknown = tuple(n for n in wanted if n not in catalog_names)

    if mode == DataSelectionMode.none.value:
        selected: list[CatalogObject] = []
    elif mode == SelectionMode.include.value:
        chosen = set(wanted)
        selected = [o for o in candidates if o.name in chosen]
    elif mode == SelectionMode.all_except.value:
        removed = set(wanted)
        selected = [o for o in candidates if o.name not in removed]
    else:  # all
        selected = list(candidates)

    includes = tuple(selection.include_patterns)
    if includes:
        selected = [
            o for o in selected if any(fnmatch.fnmatchcase(o.name, p) for p in includes)
        ]
    excludes = tuple(selection.exclude_patterns)
    if excludes:
        selected = [
            o
            for o in selected
            if not any(fnmatch.fnmatchcase(o.name, p) for p in excludes)
        ]

    return ResolvedSelection(
        objects=tuple(selected),
        excluded_internal=internal,
        unknown_names=unknown,
    )


def check_data_subset(
    structure_sel: ResolvedSelection,
    data_sel: ResolvedSelection,
    spec: ExportSpec | StructureOptions,
) -> list[str]:
    """
    Nombres seleccionados para DATOS que NO están en la selección de estructura (§5.3).

    Lista vacía = la restricción "datos ⊆ estructura" se cumple. Lo que se rechaza es la
    mezcla incoherente: pedir la estructura de 12 tablas y los datos de una 13ª que no
    está en el artefacto — un script que falla al ejecutarse.

    **Excepción decidida y documentada**: si ``scope_ddl`` y ``entity_ddl`` son ambos
    ``NONE``, la exportación es "solo datos" y la restricción **no aplica**. Es un caso de
    uso legítimo y frecuente (recargar una tabla que ya existe en el destino) y es la
    única forma que tienen ``csv``/``json``/``ndjson`` de existir: en esos formatos la
    estructura no es ejecutable.

    Acepta el ``ExportSpec`` entero o solo su ``structure``: los dos llamadores naturales
    (controller y tests) tienen uno u otro a mano.
    """
    structure = getattr(spec, "structure", spec)
    if (
        structure.scope_ddl == ScopeDdl.NONE
        and structure.entity_ddl == EntityDdl.NONE
    ):
        return []
    covered = structure_sel.keys
    return [o.name for o in data_sel.objects if (o.object_type, o.name) not in covered]


# --------------------------------------------------------------------------- #
# Resolución de opciones dependientes del MOTOR                                #
# --------------------------------------------------------------------------- #
# Un default que solo es válido en un motor no es un default: es un 422 esperando a que
# alguien mande el cuerpo vacío. ``auto`` traslada esa elección al servidor, que es el único
# que conoce el motor, y capabilities publica el valor YA RESUELTO para que el cliente y el
# servidor no puedan discrepar.

# Valor aplicable de ``sanitize.definer`` por motor cuando el spec dice ``auto``.
#   MySQL/MariaDB → ``omit``: un ``DEFINER='u'@'h'`` apuntando a un usuario que no existe en
#   el destino hace fallar el CREATE, y es el fallo más común al restaurar un volcado ajeno.
#   PostgreSQL → ``keep``: allá no hay cláusula DEFINER que quitar, así que el único valor
#   que la matriz admite es el no-op.
_DEFINER_BY_ENGINE: dict[str, DefinerMode] = {
    "mysql": DefinerMode.omit,
    "mariadb": DefinerMode.omit,
    "postgresql": DefinerMode.keep,
}


def resolve_definer(mode: DefinerMode, engine: str | None) -> DefinerMode:
    """
    Resuelve ``sanitize.definer`` a un valor CONCRETO (``keep``/``omit``/``replace``).

    Solo traduce ``auto``; cualquier otro valor se devuelve tal cual — un ``omit`` explícito
    contra PostgreSQL debe seguir llegando a la matriz para que la rechace, no resolverse en
    silencio a algo que el usuario no pidió.

    Sin motor conocido (validación en seco del cliente, antes de elegir servidor) ``auto``
    cae a ``omit``: es lo aplicable en la familia MySQL, que es el caso donde la opción
    significa algo.
    """
    if mode != DefinerMode.auto:
        return mode
    return _DEFINER_BY_ENGINE.get(str(engine or ""), DefinerMode.omit)


# --------------------------------------------------------------------------- #
# Matriz de compatibilidad (§11.1)                                             #
# --------------------------------------------------------------------------- #
# UNA sola estructura de datos alimenta el endpoint de capacidades y la validación del
# servidor. Si fueran dos, publicaríamos una promesa que el servidor no cumple — que es
# exactamente peor que no publicar nada.
#
# Vocabulario de ``forbids``:
#   "ruta.opcion"        la opción debe estar en su valor NEUTRO (apagada)
#   "ruta.opcion=valor"  la opción no puede tener ese valor
#   "structure.*"        atajo publicado por el diseño; expande a los DOS niveles de DDL
#                        (los modificadores ``drop_if_exists``/``drop_cascade`` son
#                        inertes cuando ambos niveles están en NONE, así que exigirles un
#                        valor sería ruido)
# ``requires``: la opción debe estar PRESENTE y no vacía.
# ``when`` admite la clave especial ``engine``, que no es parte del spec sino del destino.


@dataclass(frozen=True)
class Incompatibility:
    """
    Una regla de la matriz que el spec incumple.

    ``blocking=False`` es un AVISO: la combinación es legal pero tiene una consecuencia
    que el operador debe conocer antes de lanzar el job (mismo criterio que
    ``plan_integrity.PlanFinding``).
    """

    code: str
    field: str
    message: str
    blocking: bool = True
    detail: dict = field(default_factory=dict)

    @property
    def public_context(self) -> dict:
        """Lo que viaja en el 422 (§11.2): código estable + opción culpable + contexto."""
        return {"code": self.code, "field": self.field, **self.detail}


@dataclass(frozen=True)
class _Rule:
    when: Mapping[str, str]
    reason: str
    forbids: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    blocking: bool = True
    # Para las reglas que son puro aviso (sin ``forbids`` ni ``requires``) hay que decir a
    # qué opción se refiere el mensaje.
    field: str = ""


_WILDCARDS: dict[str, tuple[str, ...]] = {
    "structure.*": ("structure.scope_ddl", "structure.entity_ddl"),
}

# Valor NEUTRO ("apagado") de cada opción que alguna regla prohíbe sin igualdad.
_NEUTRAL: dict[str, Any] = {
    "structure.scope_ddl": ScopeDdl.NONE.value,
    "structure.entity_ddl": EntityDdl.NONE.value,
    "data.insert_variant": InsertVariant.none.value,
    "sanitize.session_preamble": False,
    "sanitize.transaction_wrap": False,
    "output.split_max_bytes": None,
    "output.compression": Compression.none.value,
    "output.schema_manifest": False,
}

# Enumerado de cada opción, para poder decirle al cliente QUÉ valores le quedan.
_OPTION_ENUM: dict[str, type[enum.StrEnum]] = {
    "format": Format,
    "structure.scope_ddl": ScopeDdl,
    "structure.entity_ddl": EntityDdl,
    "data.insert_variant": InsertVariant,
    "sanitize.definer": DefinerMode,
    "sanitize.autoincrement": AutoincrementMode,
    "sanitize.charset_override.mode": CharsetOverrideMode,
    "sanitize.constraints_placement": ConstraintsPlacement,
    "csv.line_terminator": LineTerminator,
    "output.organization": Organization,
    "output.compression": Compression,
    "output.delivery": Delivery,
    "output.binary_encoding": BinaryEncoding,
    "on_error": OnError,
}

_NO_STRUCTURE_REASON = (
    "El formato '{fmt}' no transporta estructura ejecutable: la definición de los objetos "
    "solo puede viajar como metadato descriptivo del manifiesto, que NO es un script y no "
    "permite restaurar nada."
)

# Opciones que solo significan algo dentro de un SCRIPT y que por lo tanto ningún formato
# de datos admite. Se listan una vez y se comparten: tener tres copias es cómo una regla se
# endurece en csv y se olvida en ndjson.
#   - ``structure.*``          → los dos niveles de DDL (el atajo lo expande ``_WILDCARDS``)
#   - ``data.insert_variant``  → INSERT/REPLACE/UPSERT son sintaxis SQL
#   - ``session_preamble``     → SET de sesión, otra vez SQL
#   - ``transaction_wrap``     → START TRANSACTION/COMMIT, ídem
#   - ``charset_override``     → reescribe CHARACTER SET/COLLATE de las DEFINICIONES; en un
#     archivo de datos la codificación es ``output.file_encoding``, que es otra cosa
_DATA_FORMAT_FORBIDS: tuple[str, ...] = (
    "structure.*",
    "data.insert_variant",
    "sanitize.session_preamble",
    "sanitize.transaction_wrap",
    f"sanitize.charset_override.mode={CharsetOverrideMode.override.value}",
)

_RULES: tuple[_Rule, ...] = (
    _Rule(
        when={"format": Format.csv.value},
        forbids=(
            *_DATA_FORMAT_FORBIDS,
            "output.organization=single",
            "output.schema_manifest",
            f"output.delivery={Delivery.inline.value}",
        ),
        reason=(
            "El formato delimitado solo transporta datos, un archivo por tabla: no admite "
            "sentencias de estructura, ni variantes de INSERT, ni preámbulo de sesión, ni "
            "juego de caracteres de las definiciones, ni manifiesto de esquema, ni un "
            "artefacto único —y por eso tampoco la entrega en línea, que exige uno solo."
        ),
    ),
    _Rule(
        when={"format": Format.json.value},
        # Un fragmento de un arreglo (o del objeto envoltorio) NO es JSON válido: partir el
        # archivo produciría trozos que ningún parser acepta. En ndjson sí se puede, porque
        # cada línea es un documento completo — de ahí que la regla sea solo para json.
        forbids=(*_DATA_FORMAT_FORBIDS, "output.split_max_bytes"),
        reason=(
            _NO_STRUCTURE_REASON.format(fmt="json")
            + " Además no se puede partir en fragmentos: un trozo de un documento JSON no "
            "es JSON válido (usá 'ndjson', que sí es partible línea a línea)."
        ),
    ),
    _Rule(
        when={"format": Format.ndjson.value},
        forbids=_DATA_FORMAT_FORBIDS,
        reason=_NO_STRUCTURE_REASON.format(fmt="ndjson"),
    ),
    _Rule(
        when={"format": Format.sql.value},
        forbids=("output.schema_manifest",),
        reason=(
            "El manifiesto de esquema es la forma en que json/ndjson describen una "
            "estructura que no pueden ejecutar. En un artefacto 'sql' la estructura ya "
            "viaja como DDL: usá las opciones de 'structure'."
        ),
    ),
    _Rule(
        when={"output.delivery": Delivery.inline.value},
        forbids=(
            "output.organization=per_object",
            "output.split_max_bytes",
            "output.compression",
        ),
        reason=(
            "La entrega en línea solo admite un artefacto único sin comprimir: es texto "
            "para copiar al portapapeles, no un archivo."
        ),
    ),
    _Rule(
        when={"output.compression": Compression.gzip.value},
        forbids=("output.organization=per_object", "output.split_max_bytes"),
        reason=(
            "gzip comprime UN flujo y no es un contenedor: un artefacto multiarchivo se "
            "entrega en zip."
        ),
    ),
    _Rule(
        when={"structure.scope_ddl": ScopeDdl.DROP_CREATE.value},
        requires=("structure.confirm_scope_drop",),
        reason=(
            "Eliminar y recrear el contenedor exige repetir su nombre como confirmación "
            "explícita."
        ),
    ),
    _Rule(
        when={"sanitize.definer": DefinerMode.replace.value},
        requires=("sanitize.definer_value",),
        reason="Reemplazar el DEFINER exige indicar con qué valor se reemplaza.",
    ),
    _Rule(
        when={"engine": "postgresql"},
        forbids=(
            f"sanitize.definer={DefinerMode.omit.value}",
            f"sanitize.definer={DefinerMode.replace.value}",
        ),
        reason=(
            "PostgreSQL no tiene cláusula DEFINER: la propiedad del objeto y SECURITY "
            "DEFINER son mecanismos distintos, así que la opción no es aplicable."
        ),
    ),
    _Rule(
        when={
            "engine": "postgresql",
            "structure.scope_ddl": ScopeDdl.CREATE_IF_NOT_EXISTS.value,
        },
        field="structure.scope_ddl",
        reason=(
            "PostgreSQL no tiene CREATE DATABASE IF NOT EXISTS en ninguna versión: el "
            "script fallaría con un error de sintaxis. Usá 'CREATE' o 'NONE'."
        ),
    ),
    _Rule(
        when={"engine": "postgresql", "structure.scope_ddl": ScopeDdl.DROP_CREATE.value},
        forbids=("sanitize.transaction_wrap",),
        reason=(
            "PostgreSQL no admite DROP DATABASE dentro de un bloque transaccional: el "
            "script fallaría al ejecutarse."
        ),
    ),
    _Rule(
        when={"engine": "postgresql", "structure.scope_ddl": ScopeDdl.DROP_CREATE.value},
        field="structure.scope_ddl",
        blocking=False,
        reason=(
            "El DROP DATABASE del script no es ejecutable desde una conexión a esa misma "
            "base: quien lo ejecute debe conectarse a otra (por ejemplo 'postgres')."
        ),
    ),
)


def compatibility_matrix() -> list[dict]:
    """
    La matriz tal como se publica en ``/export-capabilities`` (§11.1).

    Es la MISMA lista que evalúa ``validate_compatibility``: el cliente puede deshabilitar
    controles en la interfaz con la certeza de que el servidor va a rechazar exactamente
    lo mismo, ni más ni menos.
    """
    return [
        {
            "when": dict(rule.when),
            "forbids": list(rule.forbids),
            "requires": list(rule.requires),
            "reason": rule.reason,
            "blocking": rule.blocking,
            "code": CODE_INCOMPATIBLE_OPTION,
        }
        for rule in _RULES
    ]


def validate_compatibility(
    spec: ExportSpec, *, engine: str | None = None
) -> list[Incompatibility]:
    """
    Evalúa la matriz contra un spec concreto. Lista vacía = combinación admisible.

    ``engine`` es opcional porque hay dos momentos de validación: el cliente puede pedir
    la matriz y validar en seco sin haber elegido servidor, y el controller valida ya
    sabiendo el motor. Las reglas que dependen del motor **se saltean** si no se lo
    indican — nunca se evalúan "a favor" con un motor supuesto.

    Devuelve TODAS las incompatibilidades, no la primera: un formulario que corrige una
    opción por vuelta es una tortura evitable.
    """
    found: list[Incompatibility] = []
    for rule in _RULES:
        if not _when_matches(rule, spec, engine):
            continue
        fired = False
        for target in _expand(rule.forbids):
            item = _check_forbid(rule, target, spec)
            if item is not None:
                found.append(item)
                fired = True
        for target in rule.requires:
            item = _check_require(rule, target, spec)
            if item is not None:
                found.append(item)
                fired = True
        if not fired and not rule.forbids and not rule.requires:
            # Regla puramente informativa: se dispara con el ``when``.
            found.append(
                Incompatibility(
                    code=CODE_INCOMPATIBLE_OPTION,
                    field=rule.field or _first_key(rule.when),
                    message=rule.reason,
                    blocking=rule.blocking,
                    detail=dict(rule.when),
                )
            )
    found.extend(_validate_file_encoding(spec.output.file_encoding))
    if spec.format == Format.csv:
        # El dialecto delimitado no es una combinación de opciones sino la FORMA de unos
        # caracteres, así que no se puede expresar como una regla de la matriz (que compara
        # valores). Se valida acá para que el cliente reciba el MISMO error estructurado por
        # la misma puerta, en vez de un 500 al escribir la primera fila.
        found.extend(_validate_csv_dialect(spec.csv))
    return found


# Codificaciones de archivo admitidas. Es una WHITELIST y no "cualquier códec que Python
# conozca" por un motivo concreto: el artefacto se codifica POR TROZO, así que un códec con
# estado (``utf-16``, ``utf-32``, cualquier variante con BOM automático) escribe su marca de
# orden de bytes en CADA ``write`` y el archivo sale corrupto — con un sha256 que igual lo
# declara íntegro, porque el checksum se calcula sobre lo que se escribió. Las cuatro que
# quedan son sin estado y cubren los casos reales (UTF-8, UTF-8 con BOM para Excel, y las
# dos de un solo byte que piden las herramientas antiguas).
ALLOWED_FILE_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "latin-1", "cp1252")

# Alias equivalentes que Python acepta y que apuntan a un códec de la whitelist. Se
# normaliza ``_``→``-`` y a minúsculas antes de buscar acá.
_FILE_ENCODING_ALIASES: dict[str, str] = {
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "utf8-sig": "utf-8-sig",
    "utf-8-sig": "utf-8-sig",
    "latin-1": "latin-1",
    "latin1": "latin-1",
    "iso-8859-1": "latin-1",
    "cp1252": "cp1252",
    "windows-1252": "cp1252",
}


def normalize_file_encoding(value: str) -> str | None:
    """Nombre canónico de una codificación de la whitelist, o ``None`` si no está."""
    return _FILE_ENCODING_ALIASES.get(
        str(value or "").strip().lower().replace("_", "-")
    )


def _validate_file_encoding(value: str) -> list[Incompatibility]:
    if normalize_file_encoding(value) is not None:
        return []
    return [
        Incompatibility(
            code=CODE_INCOMPATIBLE_OPTION,
            field="output.file_encoding",
            message=(
                "Codificación de archivo no admitida. El artefacto se escribe por trozos y "
                "un códec con estado (utf-16, utf-32) repetiría su marca de orden de bytes "
                "en cada uno, produciendo un archivo corrupto que el checksum igual daría "
                "por íntegro."
            ),
            detail={"allowed": list(ALLOWED_FILE_ENCODINGS)},
        )
    ]


def _validate_csv_dialect(csv: CsvOptions) -> list[Incompatibility]:
    """
    Comprueba que el dialecto delimitado sea escribible **y reversible**.

    Cada regla existe por un archivo que no se puede volver a leer:

    - un separador o una comilla que no son exactamente UN carácter no se pueden detectar
      al reimportar (ningún parser de CSV admite separadores de varios caracteres de forma
      portable);
    - un separador igual a la comilla, o un escape igual a alguno de los dos, produce un
      archivo AMBIGUO: el mismo byte abre y cierra;
    - ``\\r``/``\\n`` como separador o comilla convierten cada campo en una fila;
    - un ``null_representation`` que contiene el separador, la comilla o un salto de línea
      obliga a cuotearlo, y un centinela cuoteado deja de distinguirse de la cadena literal
      —que es justo lo que la opción existe para evitar.
    """
    out: list[Incompatibility] = []

    def _bad(field: str, message: str, **detail) -> None:
        out.append(
            Incompatibility(
                code=CODE_INCOMPATIBLE_OPTION,
                field=field,
                message=message,
                detail={"format": Format.csv.value, **detail},
            )
        )

    if len(csv.delimiter) != 1 or csv.delimiter in "\r\n":
        _bad(
            "csv.delimiter",
            "El separador de campos tiene que ser exactamente un carácter, y no un salto "
            "de línea.",
        )
    if len(csv.quote_char) != 1 or csv.quote_char in "\r\n":
        _bad(
            "csv.quote_char",
            "El carácter de encomillado tiene que ser exactamente un carácter, y no un "
            "salto de línea.",
        )
    if csv.delimiter == csv.quote_char:
        _bad(
            "csv.quote_char",
            "El separador de campos y el carácter de encomillado no pueden ser el mismo: "
            "el archivo sería ambiguo.",
        )
    if csv.escape_char is not None:
        if len(csv.escape_char) != 1 or csv.escape_char in "\r\n":
            _bad(
                "csv.escape_char",
                "El carácter de escape tiene que ser exactamente un carácter, y no un "
                "salto de línea. Dejalo nulo para duplicar la comilla (RFC 4180).",
            )
        elif csv.escape_char in (csv.delimiter, csv.quote_char):
            _bad(
                "csv.escape_char",
                "El carácter de escape no puede coincidir con el separador ni con la "
                "comilla. Dejalo nulo para duplicar la comilla (RFC 4180).",
            )
    sentinel = csv.null_representation
    if sentinel and (
        csv.delimiter in sentinel
        or csv.quote_char in sentinel
        or "\r" in sentinel
        or "\n" in sentinel
    ):
        _bad(
            "csv.null_representation",
            "La representación de los nulos no puede contener el separador, la comilla ni "
            "un salto de línea: habría que cuotearla y dejaría de distinguirse de una "
            "cadena con ese mismo texto.",
        )
    return out


# --------------------------------------------------------------------------- #
# Organización, fragmentación y contenedor (§10.3)                             #
# --------------------------------------------------------------------------- #


def is_multifile(spec: ExportSpec) -> bool:
    """
    ¿El artefacto va a estar compuesto por MÁS DE UN archivo lógico?

    Dos causas, y basta una: un archivo por objeto, o la fragmentación por tamaño. Se
    responde desde el SPEC y no desde el resultado porque la decisión de empaquetado hay que
    tomarla antes de escribir el primer byte — cuando todavía no se sabe cuántos fragmentos
    van a salir. Un ``split_max_bytes`` que al final produce un solo fragmento igual viaja
    en contenedor: prometer un ``.sql`` suelto y entregar un ``.zip`` según el tamaño real
    haría que el cliente no pudiera nombrar la descarga.
    """
    return (
        spec.output.organization == Organization.per_object
        or bool(spec.output.split_max_bytes)
    )


def effective_compression(spec: ExportSpec) -> Compression:
    """
    La compresión que se aplica DE VERDAD, que no siempre es la pedida.

    §10.3: *"multiarchivo siempre se entrega dentro de un contenedor"*. Un artefacto de
    varios archivos con ``compression='none'`` no es una opción del usuario: no existe forma
    de entregar dos archivos por una descarga. Por eso acá se **eleva a zip** en vez de
    responder 422 — el 422 obligaría a pedir zip explícitamente en el caso más común
    (``format='csv'``, que la matriz ya fuerza a un archivo por tabla) y sería ceremonia sin
    decisión: no hay otra respuesta posible.

    Lo que la matriz SÍ rechaza es pedir ``gzip`` para multiarchivo: gzip comprime UN flujo
    y no es un contenedor, así que ahí el usuario pidió algo que sí tiene alternativa (zip) y
    corresponde que elija. Esta función solo eleva desde ``none``.
    """
    if is_multifile(spec):
        return Compression.zip
    return spec.output.compression


def blocking(items: Sequence[Incompatibility]) -> list[Incompatibility]:
    """Solo las incompatibilidades que impiden ejecutar (las demás son avisos)."""
    return [i for i in items if i.blocking]


def raise_for_incompatibilities(items: Sequence[Incompatibility]) -> None:
    """
    Lanza el 422 estructurado de §11.2 si hay alguna incompatibilidad BLOQUEANTE.

    Se reporta la primera en ``public_context`` (código + opción culpable + valores
    admitidos) y todas en ``fields``, para que la interfaz pueda marcarlas juntas.
    """
    hard = blocking(items)
    if not hard:
        return
    first = hard[0]
    raise AppHttpException(
        message=first.message,
        status_code=422,
        public_context={
            **first.public_context,
            "fields": [i.field for i in hard],
        },
        context={"incompatibilities": [i.message for i in hard]},
    )


def _first_key(when: Mapping[str, str]) -> str:
    for key in when:
        if key != "engine":
            return key
    return "format"


def _expand(forbids: Iterable[str]) -> list[str]:
    out: list[str] = []
    for entry in forbids:
        out.extend(_WILDCARDS.get(entry, (entry,)))
    return out


def _when_matches(rule: _Rule, spec: ExportSpec, engine: str | None) -> bool:
    for key, expected in rule.when.items():
        if key == "engine":
            # Sin motor conocido, la regla no se evalúa (ni a favor ni en contra).
            if engine is None or str(engine) != expected:
                return False
            continue
        if _value_at(spec, key) != expected:
            return False
    return True


def _check_forbid(rule: _Rule, target: str, spec: ExportSpec) -> Incompatibility | None:
    path, _, forbidden_value = target.partition("=")
    current = _value_at(spec, path)
    if forbidden_value:
        if current != forbidden_value:
            return None
        allowed = [
            v for v in _values_of(path) if v != forbidden_value
        ] or _fallback_allowed(path)
    else:
        neutral = _NEUTRAL.get(path)
        if current == neutral:
            return None
        allowed = [neutral]
    return Incompatibility(
        code=CODE_INCOMPATIBLE_OPTION,
        field=path,
        message=rule.reason,
        blocking=rule.blocking,
        detail={**dict(rule.when), "allowed": allowed},
    )


def _check_require(rule: _Rule, path: str, spec: ExportSpec) -> Incompatibility | None:
    current = _value_at(spec, path)
    if current is not None and str(current).strip() != "":
        return None
    return Incompatibility(
        code=CODE_INCOMPATIBLE_OPTION,
        field=path,
        message=rule.reason,
        blocking=rule.blocking,
        detail={**dict(rule.when), "required": True},
    )


def _values_of(path: str) -> list[Any]:
    cls = _OPTION_ENUM.get(path)
    return [m.value for m in cls] if cls else []


def _fallback_allowed(path: str) -> list[Any]:
    return [_NEUTRAL[path]] if path in _NEUTRAL else []


def _value_at(spec: ExportSpec, path: str) -> Any:
    """Lee una opción por su ruta con puntos. Los enumerados salen como su ``value``."""
    node: Any = spec
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node.value if isinstance(node, enum.Enum) else node


# --------------------------------------------------------------------------- #
# Filtro de filas por objeto (§9.2) — el punto más delicado del spec           #
# --------------------------------------------------------------------------- #
# Es la ÚNICA entrada del cliente que termina dentro de una consulta contra el origen.
# Se valida con la maquinaria que ya existe (``query_policy``), no con una segunda: un
# clasificador paralelo divergiría del que protege la consola SQL y el agujero aparecería
# en el que se actualizó menos.


def build_row_select_sql(
    dialect: str,
    table: str,
    columns: Sequence[str],
    *,
    where: str | None = None,
    order_by: Sequence[str] = (),
    limit: int | None = None,
) -> str:
    """
    Arma la consulta de lectura de UNA tabla. **Único constructor del proyecto**: lo llaman
    tanto ``validate_row_filter`` como ``export_writer._select_sql``.

    Que sea uno solo no es prolijidad: mientras el validador armaba un prefijo
    (``SELECT … WHERE {where}``) y el writer una cadena distinta
    (``… WHERE {where} ORDER BY … LIMIT …``), lo VALIDADO y lo EJECUTADO no eran lo mismo,
    y un ``where`` terminado en comentario (``1=1 --``) comentaba el ``ORDER BY`` y el
    ``LIMIT`` que el operador confirmó y que el ``confirm_token`` hasheó: se exportaba la
    tabla entera, sin orden, con un manifiesto que igual afirmaba ``deterministic: true``.

    El ``where`` va SIEMPRE entre PARÉNTESIS. Con la prohibición de comentarios de
    ``validate_row_filter`` alcanzaría, pero son dos defensas baratas e independientes: los
    paréntesis además impiden que un ``OR`` suelto se coma la precedencia de lo que venga
    detrás.
    """
    cols = ", ".join(quote_identifier(c, dialect) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {quote_identifier(table, dialect)}"
    if where:
        sql += f" WHERE ({where})"
    if order_by:
        sql += " ORDER BY " + ", ".join(quote_identifier(c, dialect) for c in order_by)
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


# Tokens que abren un comentario. Un filtro de exportación no tiene NINGÚN uso legítimo
# para un comentario, y admitirlos hacía que toda la validación dependiera de que sqlglot
# y el motor coincidieran en qué es código — y no coinciden: sqlglot NO tokeniza el
# contenido de un ``/*! … */`` (ni el de un ``/*M! … */``), así que
# ``1=1 /*!50000 UNION SELECT user,authentication_string FROM mysql.user */`` pasaba los
# cinco controles con un árbol que solo veía ``1=1``.
_COMMENT_TOKENS = ("--", "/*", "*/")
# ``#`` es comentario SOLO en la familia MySQL: en PostgreSQL es el XOR de enteros y
# rechazarlo ahí prohibiría un operador legítimo (mismo matiz que
# ``query_policy._scan_normalize``).
_MYSQL_FAMILY_DIALECTS = ("mysql", "mariadb")


def validate_row_filter(
    where: str,
    table: str,
    columns: Sequence[str],
    dialect: str,
    *,
    order_by: Sequence[str] = (),
    limit: int | None = None,
) -> None:
    """
    Valida un ``where`` arbitrario. No devuelve nada: o pasa, o lanza 422 **antes de tocar
    el motor**.

    Los cuatro pasos del §9.2, en orden:

    1. se arma la consulta REAL que se va a ejecutar —la COMPLETA, con ``ORDER BY`` y
       ``LIMIT`` incluidos y por el mismo constructor que usa el writer
       (``build_row_select_sql``)—, con los identificadores del catálogo delimitados:
       validar un fragmento suelto no sirve, y validar un PREFIJO es peor todavía porque
       da la impresión de que sí;
    2. se parsea ENTERA con sqlglot; más de una sentencia o texto ilegible ⇒ rechazo;
    3. se clasifica con ``query_policy.classify_statement``, que debe dar ``read``. Ahí
       vive la blocklist (``COPY … FROM PROGRAM``, ``lo_import``, DCL, ``pg_read_file``…)
       y el criterio fail-closed: nodo no mapeado o ``exp.Command`` ⇒ peligroso;
    4. el conjunto de tablas del AST debe ser EXACTAMENTE ``{table}`` — mismo criterio que
       ``query_policy._impact_query`` usa para descartar un COUNT que no corresponde. Esto
       corta subconsultas a otras tablas, referencias calificadas a otra base e
       ``information_schema``.

    Se agrega un quinto control que el diseño pide en intención: **cualquier subconsulta,
    CTE o UNION se rechaza**, aunque no nombre otra tabla. Un ``WHERE id IN (SELECT 1)``
    no aporta nada a un filtro de exportación y mantener el árbol plano hace que el paso 4
    sea una garantía y no una heurística.

    Y un sexto, previo a todos: **ningún token de comentario**. Ver ``_COMMENT_TOKENS``.

    El texto del filtro **nunca** se devuelve en el error: reflejar la entrada es cómo un
    payload de inyección termina renderizado en otra pantalla (mismo criterio que
    ``identifiers.validate_identifier``).
    """
    text = (where or "").strip()
    if not text:
        _reject_filter(table, "empty_filter")
    if len(text) > MAX_ROW_FILTER_CHARS:
        _reject_filter(table, "too_long", limit=MAX_ROW_FILTER_CHARS)

    tokens = _COMMENT_TOKENS + (
        ("#",) if dialect in _MYSQL_FAMILY_DIALECTS else ()
    )
    for token in tokens:
        if token in text:
            _reject_filter(table, "comment_not_allowed")

    sql = build_row_select_sql(
        dialect, table, columns, where=text, order_by=order_by, limit=limit
    )

    # Se usa el mapeo de dialecto de ``query_policy`` (privado, mismo paquete) a
    # propósito: dos tablas motor->dialecto de sqlglot podrían divergir y el clasificador
    # estaría leyendo la consulta con otra gramática que este parseo.
    read_dialect = query_policy._sqlglot_dialect(dialect)
    try:
        trees = sqlglot.parse(sql, read=read_dialect)
    except Exception:  # noqa: BLE001 — sqlglot lanza varias familias de error
        _reject_filter(table, "unparseable")
    if len(trees) != 1 or trees[0] is None:
        # Un ``;`` en el filtro convierte la consulta en un lote. No se ejecuta jamás.
        _reject_filter(table, "multiple_statements")
    tree = trees[0]

    plan = query_policy.classify_statement(sql, engine=dialect)
    if plan.danger != query_policy.READ:
        _reject_filter(
            table,
            "not_read_only",
            danger=plan.danger,
            reasons=[r.code for r in plan.reasons][:5],
        )

    for node in tree.find_all(exp.Select, exp.Subquery, exp.CTE, exp.Union):
        if node is not tree:
            _reject_filter(table, "subquery_not_allowed")

    referenced = {(t.db or "", t.name) for t in tree.find_all(exp.Table)}
    if referenced != {("", table)}:
        _reject_filter(table, "foreign_table_reference")

    # Un CALIFICADOR de columna que nombra otra base o tabla (``otra_db.orders.id``) NO
    # produce un nodo ``exp.Table``, así que el conjunto de tablas de arriba no lo ve. El
    # motor lo rechazaría por columna desconocida, pero un 422 acá es un mensaje útil en
    # vez de un error de driver a mitad del job — y deja el árbol acotado a esta tabla sin
    # depender de cómo resuelva nombres cada motor.
    for column in tree.find_all(exp.Column):
        if column.args.get("catalog") or column.args.get("db"):
            _reject_filter(table, "foreign_column_qualifier")
        qualifier = column.args.get("table")
        if qualifier is not None and str(qualifier.name or "") not in ("", table):
            _reject_filter(table, "foreign_column_qualifier")


def _reject_filter(table: str, reason: str, **detail) -> NoReturn:
    raise AppHttpException(
        message=(
            f"El filtro de filas de '{table}' no es una condición de lectura simple "
            "sobre esa misma tabla."
        ),
        status_code=422,
        public_context={
            "code": CODE_INVALID_ROW_FILTER,
            "field": f"data.per_object.{table}.where",
            "table": table,
            "reason": reason,
            **detail,
        },
    )


# --------------------------------------------------------------------------- #
# Nombre del artefacto (§9.2)                                                  #
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"\{([^{}]*)\}")


def sanitize_filename(name: str, *, fallback: str = "export") -> str:
    """
    Deja solo alfanuméricos y ``.-_`` (el resto → ``_``) para un nombre de archivo seguro.

    Criterio ÚNICO del proyecto: lo comparte con la descarga de schema-comparisons, que
    antes lo tenía duplicado. Neutraliza de paso el recorrido de rutas (``../``, ``/``,
    ``\\`` y los dos puntos de una unidad de Windows caen todos en ``_``), que es la razón
    real por la que el cliente nunca envía ni recibe una ruta.
    """
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(name or ""))
    return safe.strip("._")[:MAX_FILENAME_CHARS].strip("._") or fallback


def sanitize_filename_template(template: str, tokens: Mapping[str, str]) -> str:
    """
    Sustituye los tokens de la whitelist en la plantilla y sanea el resultado.

    La whitelist (``{database} {object} {date} {time} {job_id}``) es **cerrada**: un token
    desconocido es un 422, nunca una sustitución vacía silenciosa — una plantilla que el
    usuario cree que funciona y produce ``tienda--`` es un reporte de bug garantizado.
    Las llaves sueltas también se rechazan por lo mismo.

    Un token válido que el llamador no provee se sustituye por cadena vacía: ``{object}``
    solo tiene valor en ``organization=per_object`` y no tiene sentido exigirlo siempre.
    Es responsabilidad del llamador pasar ``object`` cuando emite un archivo por objeto —
    si no, todos los archivos compartirían nombre.

    El saneado se aplica al resultado COMPLETO, después de sustituir: así un valor de
    token con ``../`` queda neutralizado aunque la plantilla fuera inocente.
    """
    text = (template or "").strip() or DEFAULT_FILENAME_TEMPLATE
    unknown = sorted(
        {m.group(1) for m in _TOKEN_RE.finditer(text)} - set(FILENAME_TOKENS)
    )
    stray = _TOKEN_RE.sub("", text)
    if unknown or "{" in stray or "}" in stray:
        raise AppHttpException(
            message="La plantilla del nombre de archivo usa tokens no permitidos.",
            status_code=422,
            public_context={
                "code": CODE_INCOMPATIBLE_OPTION,
                "field": "output.filename_template",
                "unknown_tokens": unknown,
                "allowed": [f"{{{t}}}" for t in FILENAME_TOKENS],
            },
        )
    rendered = _TOKEN_RE.sub(lambda m: str(tokens.get(m.group(1), "") or ""), text)
    return sanitize_filename(rendered)


__all__ = [
    "ALLOWED_FILE_ENCODINGS",
    "BINARY_ENCODINGS",
    "CODE_ARTIFACT_CONSUMED",
    "CODE_ARTIFACT_EXPIRED",
    "CODE_DATA_WITHOUT_STRUCTURE",
    "CODE_FINGERPRINT_CHANGED",
    "CODE_INCOMPATIBLE_OPTION",
    "CODE_INLINE_TOO_LARGE",
    "CODE_INVALID_ROW_FILTER",
    "CODE_MISSING_DEPENDENCIES",
    "CODE_QUOTA_EXCEEDED",
    "CODE_SCOPE_NOT_ALLOWED",
    "DEFAULT_FILENAME_TEMPLATE",
    "ERROR_CODES",
    "FILENAME_TOKENS",
    "MAX_FILENAME_CHARS",
    "MAX_ROW_FILTER_CHARS",
    "AutoincrementMode",
    "BinaryEncoding",
    "CatalogObject",
    "CharsetOverride",
    "CharsetOverrideMode",
    "Compression",
    "ConstraintsPlacement",
    "CsvOptions",
    "DataOptions",
    "DataSelectionMode",
    "DefinerMode",
    "Delivery",
    "EntityDdl",
    "ExportSpec",
    "Format",
    "Incompatibility",
    "InsertVariant",
    "LineTerminator",
    "OnError",
    "Organization",
    "OutputOptions",
    "ResolvedSelection",
    "RowFilter",
    "SanitizeOptions",
    "ScopeDdl",
    "Selection",
    "SelectionMode",
    "StructureOptions",
    "blocking",
    "build_row_select_sql",
    "check_data_subset",
    "compatibility_matrix",
    "effective_compression",
    "is_multifile",
    "normalize_file_encoding",
    "raise_for_incompatibilities",
    "resolve_definer",
    "resolve_selection",
    "sanitize_filename",
    "sanitize_filename_template",
    "validate_compatibility",
    "validate_row_filter",
]
