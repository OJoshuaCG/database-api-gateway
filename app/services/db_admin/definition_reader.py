"""
Núcleo PURO de ``get_definition``: de una lectura cruda del adapter a un resultado entregable.

``build_definition`` recibe la ``DefinitionRead`` que produjo el adapter y decide, sin tocar el
motor, qué se puede entregar: saca el DEFINER, redacta credenciales, mide el tamaño, calcula la
huella y fija la disponibilidad. No hay I/O, así que cada regla se prueba sin una base.

DISPONIBILIDAD EXPLÍCITA, NUNCA UN ÉXITO VACÍO
----------------------------------------------
Cada objeto sale como disponible (con cuerpo no vacío) o no disponible (sin cuerpo y con una razón
del vocabulario cerrado ``DefinitionUnavailableReason``). Un cuerpo NULL, vacío o en blanco es
"no disponible": entregar ``""`` como éxito le diría al agente que el objeto está vacío cuando el
motor simplemente no se lo mostró a esta credencial.

POR QUÉ SE RECHAZA Y NO SE TRUNCA (``too_large``)
-------------------------------------------------
Un cuerpo cortado es peor que uno ausente: el agente razonaría sobre código incompleto creyéndolo
entero. Más de ``MAX_DEFINITION_BYTES`` (medido sobre el JSON, que es lo que viaja y lo que cuenta
contra el presupuesto de 512 KiB del dispatcher) vuelve como ``too_large`` con su tamaño.

El dispatcher cuenta cada resultado DOS veces contra esos 512 KiB: una en el bloque de texto y otra
en ``structuredContent``. Por eso ``MAX_DEFINITIONS_PER_CALL`` x ``MAX_DEFINITION_BYTES`` x 2 =
3 x 64 KiB x 2 = 384 KiB, y los 128 KiB restantes cubren el envelope y los metadatos. Con 5 objetos
el peor caso eran 640 KiB y la llamada habría fallado con ``mcp.result_too_large``.

LA CUENTA DEL DEFINER NUNCA SALE
--------------------------------
Se sacan las cláusulas ``DEFINER=`` del cuerpo y de ellas solo se conserva el MODO (``security``:
``definer``/``invoker``). El nombre de la cuenta revela usuarios y hosts del motor de un tercero.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.definition_redaction import RedactionResult, redact_definition
from app.services.db_admin.dtos import DefinitionRead
from app.services.db_admin.schema_diff import normalize_body

#: Objetos por llamada: 3 x 64 KiB x 2 (el dispatcher cuenta el resultado en el texto y en
#: ``structuredContent``) = 384 KiB, por debajo del presupuesto de 512 KiB del dispatcher.
MAX_DEFINITIONS_PER_CALL = 3
#: Tope del cuerpo ya redactado, medido sobre su codificación JSON (comillas y escapes incluidos).
MAX_DEFINITION_BYTES = 65536

DefinitionAvailabilityReason = Literal[
    "insufficient_privilege", "engine_unsupported", "scope_disabled", "flag_off", "too_large"
]

_SQL_SECURITY_RE = re.compile(r"\bSQL\s+SECURITY\s+(DEFINER|INVOKER)\b", re.IGNORECASE)
_DEFINER_CLAUSE_RE = re.compile(r"\bDEFINER\s*=", re.IGNORECASE)
_KINDS_WITH_SECURITY_MODE = frozenset({"view", "routine"})


@dataclass(frozen=True)
class DefinitionResult:
    """
    Lo entregable de un objeto, ya redactado y medido. El mapeador de la tool lo pasa a
    ``DefinitionOut`` campo por campo; este tipo NO se serializa directo.
    """

    kind: str
    name: str
    routine_kind: str | None
    identity_arguments: str | None
    body_available: bool
    unavailable_reason: DefinitionAvailabilityReason | None
    body: str | None
    size_bytes: int | None
    body_fingerprint: str | None
    security: str | None
    check_option: str | None
    trigger_table: str | None
    trigger_timing: str | None
    trigger_events: list[str]
    event_schedule: str | None
    event_status: str | None
    redactions: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, int] = field(default_factory=dict)


def json_encoded_size(text: str) -> int:
    """
    Bytes del texto como string JSON (UTF-8, sin escapar no-ASCII). Es la medida del tope: lo que
    pesa en la respuesta, no los caracteres del cuerpo.
    """
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def body_fingerprint(redacted_text: str) -> str:
    """
    SHA-256 hex del cuerpo NORMALIZADO con ``schema_diff.normalize_body`` (sin DEFINER, espacios
    colapsados, sin ``;`` final): dos cuerpos que difieren solo en eso tienen la misma huella. La
    MISMA normalización que usa el diff, para que "igual huella" signifique "el diff los ve
    iguales".
    """
    return hashlib.sha256(normalize_body(redacted_text).encode("utf-8")).hexdigest()


def _security_mode(read: DefinitionRead, raw_body: str) -> str | None:
    """
    Modo ``definer``/``invoker``. El adapter lo da cuando lo conoce; si no, se infiere del cuerpo
    ORIGINAL (antes de quitar el DEFINER) para vistas y rutinas. Solo el modo, nunca la cuenta.
    """
    if read.security is not None:
        return read.security
    if read.kind not in _KINDS_WITH_SECURITY_MODE:
        return None
    explicit = _SQL_SECURITY_RE.search(raw_body)
    if explicit is not None:
        return explicit.group(1).lower()
    if _DEFINER_CLAUSE_RE.search(raw_body) is not None:
        return "definer"
    return None


def _unavailable_reason(
    read: DefinitionRead, missing_body_reason: DefinitionAvailabilityReason | None
) -> DefinitionAvailabilityReason:
    """
    ``engine_unsupported`` del adapter se respeta (PostgreSQL no tiene events: ninguna versión ni
    bandera lo arregla). Para el resto, el motivo por versión que calculó el llamador
    (``routine_body_reason``) gana sobre el genérico ``insufficient_privilege``.
    """
    if read.unavailable_reason == "engine_unsupported":
        return "engine_unsupported"
    if missing_body_reason is not None:
        return missing_body_reason
    return "insufficient_privilege"


def build_definition(
    read: DefinitionRead,
    *,
    missing_body_reason: DefinitionAvailabilityReason | None = None,
    redact: Callable[[str], RedactionResult] = redact_definition,
) -> DefinitionResult:
    """
    Convierte una lectura cruda en un ``DefinitionResult``. PURA.

    ``missing_body_reason``: motivo por versión/bandera para un cuerpo ausente (lo calcula el
    llamador con ``readonly_probe.routine_body_reason``); se ignora si el cuerpo está.
    ``redact`` es inyectable para probar el tope y la huella sin depender de los patrones.
    """
    raw_body = read.body
    has_body = raw_body is not None and bool(raw_body.strip()) and read.unavailable_reason is None
    if not has_body:
        return _unavailable_result(read, _unavailable_reason(read, missing_body_reason), None)

    assert raw_body is not None  # lo garantiza has_body; el assert es para el chequeo de tipos
    security = _security_mode(read, raw_body)
    without_definer = ServerAdapter._strip_definer_clause(raw_body)
    redaction = redact(without_definer)
    size_bytes = json_encoded_size(redaction.text)
    if size_bytes > MAX_DEFINITION_BYTES:
        return _unavailable_result(read, "too_large", size_bytes, security=security)

    return DefinitionResult(
        kind=read.kind,
        name=read.name,
        routine_kind=read.routine_kind,
        identity_arguments=read.identity_arguments,
        body_available=True,
        unavailable_reason=None,
        body=redaction.text,
        size_bytes=size_bytes,
        body_fingerprint=body_fingerprint(redaction.text),
        security=security,
        check_option=read.check_option,
        trigger_table=read.trigger_table,
        trigger_timing=read.trigger_timing,
        trigger_events=list(read.trigger_events),
        event_schedule=read.event_schedule,
        event_status=read.event_status,
        redactions=dict(redaction.redactions),
        flagged=dict(redaction.flagged),
    )


def _unavailable_result(
    read: DefinitionRead,
    reason: DefinitionAvailabilityReason,
    size_bytes: int | None,
    *,
    security: str | None = None,
) -> DefinitionResult:
    """
    Resultado sin cuerpo. Los conteos de redacción van vacíos a propósito: informar cuántas
    credenciales tenía un cuerpo que NO se entrega filtraría algo de él.
    """
    return DefinitionResult(
        kind=read.kind,
        name=read.name,
        routine_kind=read.routine_kind,
        identity_arguments=read.identity_arguments,
        body_available=False,
        unavailable_reason=reason,
        body=None,
        size_bytes=size_bytes,
        body_fingerprint=None,
        security=security if security is not None else read.security,
        check_option=read.check_option,
        trigger_table=read.trigger_table,
        trigger_timing=read.trigger_timing,
        trigger_events=list(read.trigger_events),
        event_schedule=read.event_schedule,
        event_status=read.event_status,
    )
