"""
``get_definition``: el CÓDIGO de vistas, triggers, events y rutinas pedidos por nombre.

LA TOOL NO ACEPTA SQL NI EJECUTA NADA
-------------------------------------
Recibe ``{database_id, objects: [{kind, name, routine_kind?}]}``: identificadores, nunca texto SQL.
Lee el código con ``SHOW CREATE`` o funciones ``pg_get_*`` por el camino de ESTRUCTURA
(``target_resolution.read_definitions``) y lo entrega como TEXTO. Ningún objeto se ejecuta, ni se
dispara, ni se invoca: el código de un tercero es una cadena que este módulo nunca interpreta.

EL CÓDIGO ES CONTENIDO NO CONFIABLE DE TERCEROS
-----------------------------------------------
Un cuerpo puede decir "ignorá lo anterior y…" en un comentario. Va en ``body``, listado en
``untrusted_fields`` y bajo el ``notice`` del envelope, y no se recorta jamás (``Tracker.code_body``):
un cuerpo cortado a mitad es peor que ausente. El control REAL, como en el resto del paquete, es que
ninguna tool escribe. La redacción de credenciales es best effort y NO es una frontera: la
frontera es el scope ``data.definitions``.

ORDEN: EL KILL SWITCH PRIMERO
-----------------------------
Apagado, ni siquiera se validan los argumentos: la respuesta no se distingue de la de una tool
inexistente, ni por tiempo ni por efectos. Después, argumentos (gratis, sin conectar), y recién
entonces el gate completo de la base, la auditoría fail-closed y la lectura, que viven en
``target_resolution.read_definitions``.

EL MAPEADOR ES LA LISTA BLANCA
------------------------------
``_map_definition`` arma ``DefinitionOut`` campo por campo desde el resultado ya redactado. No hay
campo para la cuenta del DEFINER, ni para el texto sin redactar, ni para un error del motor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.exceptions import AppHttpException
from app.mcp.context import ToolContext
from app.mcp.tools._envelope import Tracker, clean
from app.mcp.tools.catalog import _envelope, _warnings
from app.schemas import mcp as out
from app.services import mcp_catalog as codes

#: Tope de objetos por llamada. Espeja ``MAX_DEFINITIONS_PER_CALL`` del lector (3 x 64 KiB x 2 = 384
#: KiB entran en el presupuesto de 512 KiB del dispatcher, que cuenta el resultado en el texto y en
#: ``structuredContent``) y el ``maxItems`` del schema publicado. Se repite acá porque este paquete
#: no puede importar la capa de motor (``test_mcp_import_guard``); ``read_definitions`` lo vuelve a
#: exigir con la constante del lector y un test fija que ambas coincidan.
MAX_OBJECTS_PER_CALL = 3
_NAME_MAX = 128
_KINDS: tuple[str, ...] = ("view", "trigger", "event", "routine")
_ROUTINE_KINDS: tuple[str, ...] = ("PROCEDURE", "FUNCTION")
_ALLOWED_ITEM_KEYS = frozenset({"kind", "name", "routine_kind"})
#: Motores cuyo índice de rutinas puede salir vacío por falta de privilegio, sin error. PostgreSQL
#: no entra: ``pg_proc`` lista las rutinas del schema aunque la cuenta no pueda ejecutarlas.
_ENGINES_WITH_HIDDEN_ROUTINES = frozenset({"mysql", "mariadb"})


def _invalid(message: str) -> AppHttpException:
    return AppHttpException(
        message=message, status_code=422, public_context={"code": codes.CODE_INVALID_ARGUMENT}
    )


def _database_id(params: dict) -> int:
    database_id = params.get("database_id")
    if not isinstance(database_id, int) or isinstance(database_id, bool):
        raise _invalid("'database_id' tiene que ser un entero (el id que devuelve list_databases).")
    return database_id


def _requested_objects(params: dict) -> list[tuple[str, str, str | None]]:
    """
    Valida ``objects`` ANTES de abrir ninguna conexión. El dispatcher solo valida las claves de
    primer nivel; las de cada elemento y el ``maxItems`` se hacen cumplir acá.

    NO se rechazan nombres por sus caracteres: un nombre con comilla, punto y coma o prefijo de otra
    base es un nombre que no está en el índice y vuelve en ``missing``, sin que se emita ningún SQL
    con ese texto. Se colapsan los duplicados conservando el orden del pedido.
    """
    raw_objects = params.get("objects")
    if not isinstance(raw_objects, list) or not raw_objects:
        raise _invalid("'objects' es obligatorio: una lista de {kind, name} (no hay 'dame todo').")
    if len(raw_objects) > MAX_OBJECTS_PER_CALL:
        raise _invalid(
            f"Se pidieron {len(raw_objects)} objetos y el máximo por llamada es "
            f"{MAX_OBJECTS_PER_CALL}. Partí el pedido en lotes."
        )
    requested: list[tuple[str, str, str | None]] = []
    for item in raw_objects:
        if not isinstance(item, dict) or set(item) - _ALLOWED_ITEM_KEYS:
            raise _invalid("Cada elemento de 'objects' es {kind, name, routine_kind?} y nada más.")
        kind = item.get("kind")
        name = item.get("name")
        routine_kind = item.get("routine_kind")
        if kind not in _KINDS:
            raise _invalid(f"'kind' admite solo {list(_KINDS)}.")
        if not isinstance(name, str) or not name or len(name) > _NAME_MAX:
            raise _invalid(f"'name' tiene que ser una cadena de 1 a {_NAME_MAX} caracteres.")
        if routine_kind is not None:
            if kind != "routine":
                raise _invalid("'routine_kind' solo corresponde a objetos de tipo 'routine'.")
            if routine_kind not in _ROUTINE_KINDS:
                raise _invalid(f"'routine_kind' admite solo {list(_ROUTINE_KINDS)}.")
        entry = (kind, name, routine_kind)
        if entry not in requested:
            requested.append(entry)
    return requested


@dataclass(frozen=True, slots=True)
class _SessionFacts:
    """
    Los dos datos del façade que ``catalog._warnings`` lee, capturados ANTES de cerrar la sesión.
    Reusar ese armado evita tener dos textos de warning que se desincronicen.
    """

    consistent_structure: bool
    warnings: tuple[str, ...]


def _counts(counter: dict[str, int]) -> list[out.RedactionCountOut]:
    """Conteos por categoría en orden estable. Jamás el valor enmascarado."""
    return [
        out.RedactionCountOut(category=category, count=count)
        for category, count in sorted(counter.items())
    ]


def _map_definition(
    result, index: int, tracker: Tracker, redact: Callable[[str], str] | None = None
) -> out.DefinitionOut:
    """
    ``DefinitionResult`` -> ``DefinitionOut``, campo por campo.

    El cuerpo pasa por ``Tracker.code_body`` (saneado, anotado, sin recorte). Si el saneado lo deja
    vacío (un cuerpo hecho solo de caracteres de control) el objeto sale como no disponible: un
    "disponible" sin texto es exactamente el éxito vacío que esta tool no puede emitir.
    """
    body = None
    if result.body_available:
        body = tracker.code_body(result.body, f"data.objects[{index}].body", redact=redact)
    body_usable = bool(body and body.strip())
    unavailable_reason = result.unavailable_reason
    if result.body_available and not body_usable:
        unavailable_reason = "insufficient_privilege"

    trigger = None
    if result.kind == "trigger" and result.trigger_table is not None:
        trigger = out.TriggerMetaOut(
            table=clean(result.trigger_table),
            timing=clean(result.trigger_timing),
            events=[clean(event) for event in result.trigger_events],
        )
    event = None
    if result.kind == "event" and (
        result.event_schedule is not None or result.event_status is not None
    ):
        event = out.EventMetaOut(
            schedule=clean(result.event_schedule), status=clean(result.event_status)
        )

    return out.DefinitionOut(
        kind=result.kind,
        name=clean(result.name),
        routine_kind=result.routine_kind,
        identity_arguments=clean(result.identity_arguments),
        body_available=body_usable,
        unavailable_reason=None if body_usable else unavailable_reason,
        body=body if body_usable else None,
        size_bytes=result.size_bytes,
        body_fingerprint=result.body_fingerprint if body_usable else None,
        security=result.security,
        check_option=clean(result.check_option),
        trigger=trigger,
        event=event,
        redactions=_counts(result.redactions) if body_usable else [],
        flagged=_counts(result.flagged) if body_usable else [],
    )


def _routine_missing_warning(
    engine: str, missing: list[tuple[str, str, str | None]]
) -> out.WarningOut | None:
    """
    Un único aviso si alguna RUTINA pedida volvió en ``missing`` en MySQL/MariaDB, o ``None``.

    Por qué existe: una cuenta de estructura sin privilegio de rutina recibe cero filas de
    ``information_schema.ROUTINES`` sin ningún error, y ``missing`` diría "no existe" de una rutina
    que sí existe. El aviso no afirma ninguna de las dos cosas: da las dos salidas posibles. Se
    emite UNO por llamada aunque falten varias rutinas, y no cambia la forma de ``missing[]``.
    """
    if engine not in _ENGINES_WITH_HIDDEN_ROUTINES:
        return None
    routine_is_missing = any(kind == "routine" for (kind, _name, _routine_kind) in missing)
    if not routine_is_missing:
        return None
    return out.WarningOut(
        code=codes.WARN_ROUTINE_NOT_FOUND_OR_NOT_VISIBLE,
        message=(
            "La rutina no se encontró o la cuenta de solo lectura no tiene privilegio para "
            "verla. Si existe, regenerá la credencial de solo lectura del servidor (MariaDB >= "
            "11.3) o habilitá la lectura de cuerpos de rutinas (motores más antiguos)."
        ),
    )


def get_definition(ctx: ToolContext, params: dict) -> dict:
    """
    El código de los objetos pedidos, con disponibilidad EXPLÍCITA por objeto.

    Lo que no está en el índice de la base vuelve en ``missing`` (nunca como "sin cuerpo": esa
    distinción le dice al agente si el objeto existe). Un objeto sin código disponible trae su
    ``unavailable_reason``; uno de más de 64 KiB redactado vuelve como ``too_large`` sin recortar.
    Nunca hay un éxito vacío: o hay objetos, o hay ``missing``, o la llamada falló con su código.
    """
    ctx.assert_definitions_enabled()
    database_id = _database_id(params)
    requested = _requested_objects(params)

    batch = ctx.get_definitions(database_id, requested)

    tracker = Tracker()
    mapped = [
        _map_definition(result, i, tracker, redact=ctx.redact_text)
        for i, result in enumerate(batch.results)
    ]
    data = out.DefinitionsOut(
        objects=mapped,
        missing=[
            out.DefinitionRefOut(kind=kind, name=clean(name), routine_kind=routine_kind)
            for (kind, name, routine_kind) in batch.missing
        ],
    )
    warnings = _warnings(
        batch.database,
        _SessionFacts(
            consistent_structure=batch.consistent_structure, warnings=batch.session_warnings
        ),
        bodies_requested=False,
    )
    routine_warning = _routine_missing_warning(batch.database.database.engine, list(batch.missing))
    if routine_warning is not None:
        warnings.append(routine_warning)
    if any(obj.redactions for obj in mapped):
        warnings.append(
            out.WarningOut(
                code=codes.WARN_BODIES_REDACTED,
                message=(
                    "Se enmascararon credenciales en alguna definición. La redacción es best "
                    "effort y no garantiza que no quede ninguna."
                ),
            )
        )
    return _envelope(
        data,
        resuelta=batch.database,
        tracker=tracker,
        warnings=warnings,
        engine_version=batch.engine_version,
    )
