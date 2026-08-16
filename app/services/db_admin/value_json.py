"""
Normalización de valores del driver a algo serializable a JSON.

Extraído de ``query_runner`` (consola SQL) porque hay DOS consumidores con el mismo
problema exacto y ninguna razón para tener dos criterios:

- la **consola SQL**, que devuelve filas por la API;
- la **captura de resultados de SELECT** dentro de una migración de blueprint
  (``migration_results``), que las persiste cifradas en la BD del gateway.

Un serializador paralelo sería una fuente garantizada de divergencias (un ``Decimal``
que en un camino sale con precisión y en el otro como float, un ``timedelta`` de MySQL
rendeado como ``-1 day, 23:00:00``, un BLOB volcado entero). Los topes de PROFUNDIDAD y
CARDINALIDAD viven acá porque protegen memoria del proceso, no la presentación.
"""

from datetime import date, datetime, time as time_cls, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

# Techos DUROS de contenedores (JSON/JSONB/arrays). ``max_chars`` acota cada HOJA, no el
# total: un documento con miles de cadenas cortas se serializaría entero sin estos topes.
MAX_CONTAINER_ITEMS = 200
MAX_CONTAINER_DEPTH = 8


def format_timedelta(value: timedelta) -> str:
    """
    ``timedelta`` -> ``[-]HH:MM:SS``, el criterio ÚNICO del proyecto para el ``TIME``.

    El tipo ``TIME`` de MySQL/MariaDB llega al driver como ``timedelta`` y admite valores
    negativos y mayores a 24 h. ``str()`` los rendea como ``-1 day, 23:00:00`` (para
    ``TIME '-01:00:00'``) o ``34 days, 22:00:00`` (para ``TIME '838:00:00'``): formalmente
    correcto, inservible para un humano y **no re-insertable** como literal SQL. Se rearma
    a mano desde ``total_seconds()``, sin normalizar las horas a 24 (``838:00:00`` es un
    valor legal del tipo).

    Vive acá y no duplicado en cada consumidor porque ya hay tres (consola SQL, captura de
    resultados de migración y el render de literales de ``sql_literals``): dos criterios
    distintos para el mismo tipo serían una divergencia garantizada.
    """
    total = int(value.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def json_value(value: Any, max_chars: int, depth: int = 0) -> Any:
    """
    Convierte un valor del driver a algo serializable a JSON, recortando celdas grandes.

    ``Decimal`` se pasa a ``str`` a propósito (un float perdería precisión, que es justo
    lo que se está inspeccionando). Los binarios se muestran en hexadecimal con marca de
    recorte: ni una consola ni un informe de migración deben volcar un BLOB entero.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity no son JSON válido.
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        text = raw.hex()
        if len(text) > max_chars:
            return f"0x{text[:max_chars]}… ({len(raw)} bytes)"
        return f"0x{text}"
    if isinstance(value, (datetime, date, time_cls)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return format_timedelta(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, (list, tuple)):
        if depth >= MAX_CONTAINER_DEPTH:
            return f"[…{len(value)} elementos]"
        head = [json_value(v, max_chars, depth + 1) for v in value[:MAX_CONTAINER_ITEMS]]
        if len(value) > MAX_CONTAINER_ITEMS:
            head.append(f"… ({len(value)} elementos en total)")
        return head
    if isinstance(value, dict):
        if depth >= MAX_CONTAINER_DEPTH:
            return f"{{…{len(value)} claves}}"
        items = list(value.items())[:MAX_CONTAINER_ITEMS]
        out = {str(k): json_value(v, max_chars, depth + 1) for k, v in items}
        if len(value) > MAX_CONTAINER_ITEMS:
            out["…"] = f"({len(value)} claves en total)"
        return out
    if isinstance(value, str):
        if len(value) > max_chars:
            return f"{value[:max_chars]}… (truncado, {len(value)} caracteres)"
        return value
    text = str(value)
    return text if len(text) <= max_chars else f"{text[:max_chars]}…"


__all__ = [
    "MAX_CONTAINER_DEPTH",
    "MAX_CONTAINER_ITEMS",
    "format_timedelta",
    "json_value",
]
