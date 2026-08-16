"""
Render de VALORES como literales (SEGURIDAD CRÍTICA) — módulo PURO, sin motor ni I/O.

Extraído de ``snapshot_data`` (datos-semilla de un blueprint) porque hay más de un
consumidor con el mismo problema exacto y ninguna razón para tener dos criterios:

- **datos-semilla** de un snapshot: los valores se persisten como literales dentro del
  ``up_sql`` de una migración y luego se ejecutan sin parametrizar;
- **exportación de bases de datos**: los ``INSERT`` del artefacto son TEXTO que un humano
  descarga y ejecuta contra otro servidor; nadie va a parametrizarlos nunca.

En ambos casos el valor viaja interpolado en el SQL, así que esto **es** la superficie de
inyección. De ahí las dos reglas que gobiernan el módulo:

1. **Manejo tipado exhaustivo y fail-closed.** Un tipo que no está contemplado NO se
   serializa "a lo que salga" con ``str(value)``: lanza ``UnsupportedValueError`` y el
   consumidor omite el objeto. Un ``str()`` de conveniencia sobre un tipo de driver
   desconocido es exactamente cómo se cuela un literal mal escapado.
2. **Todo lo que termina entre comillas pasa por ``quote_string_literal``**, que ya cubre
   ``NO_BACKSLASH_ESCAPES`` de MySQL y el ``E'…'`` de PostgreSQL. El byte nulo se rechaza.

``snapshot_data`` reexporta ``render_value``/``UnsupportedValueError`` para no romper a sus
consumidores; el contrato de la semilla (PK obligatoria, techos 5000 filas / 5 MB) sigue
viviendo allá y no lo toca este módulo.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from app.services.db_admin.identifiers import quote_string_literal
from app.services.db_admin.value_json import format_timedelta

# Codificaciones admitidas para los binarios en los formatos de TEXTO (csv/json/ndjson),
# donde ``x'…'`` / ``decode(…)`` no significan nada. Se publica en capabilities.
BINARY_ENCODINGS: tuple[str, ...] = ("hex", "base64")


class UnsupportedValueError(Exception):
    """Un valor de tipo no soportado para render como literal (fail-closed → skip)."""


def render_value(value, dialect: str) -> str:
    """
    Renderiza un valor Python como literal SQL seguro para ``dialect``. Tipos no
    soportados → ``UnsupportedValueError`` (fail-closed). NUNCA interpola sin escapar.

    Notas de tipos que parecen detalles y no lo son:

    - ``bool`` va ANTES que ``int`` (en Python ``True`` es un ``int``) y se rendea
      ``TRUE/FALSE`` en PostgreSQL, ``1/0`` en la familia MySQL.
    - ``Decimal`` se rendea con ``str``, nunca por punto flotante: un ``DECIMAL(30,10)``
      pasado por ``float`` pierde dígitos en silencio y el artefacto deja de reproducir
      el origen.
    - ``datetime`` va antes que ``date`` porque es su subclase.
    - ``timedelta`` es el ``TIME`` de MySQL/MariaDB, que admite negativos y más de 24 h;
      se rendea con el criterio COMPARTIDO de ``value_json.format_timedelta``.
    - ``float`` no finito y ``Decimal`` no finito abortan: ``NaN``/``Infinity`` no tienen
      literal portable y escribirlos produce un script que el motor destino rechaza.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if dialect == "postgresql":
            return "TRUE" if value else "FALSE"
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise UnsupportedValueError("float no finito")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedValueError("decimal no finito")
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        hexs = bytes(value).hex()
        # PG: decode(...,'hex') es independiente de standard_conforming_strings.
        return f"decode('{hexs}', 'hex')" if dialect == "postgresql" else f"x'{hexs}'"
    if isinstance(value, datetime):
        return quote_string_literal(value.isoformat(sep=" "), dialect)
    if isinstance(value, (date, time)):
        return quote_string_literal(value.isoformat(), dialect)
    if isinstance(value, timedelta):
        return quote_string_literal(format_timedelta(value), dialect)
    if isinstance(value, UUID):
        return quote_string_literal(str(value), dialect)
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        s = value
    else:
        raise UnsupportedValueError(type(value).__name__)
    # El byte nulo se rechaza como skip (consistente con los tipos no soportados), no
    # como 422 que abortaría toda la petición.
    if "\x00" in s:
        raise UnsupportedValueError("null_byte")
    return quote_string_literal(s, dialect)


def render_value_text(value, *, binary_encoding: str) -> str:
    """
    Renderiza un valor como TEXTO plano para los formatos no ejecutables (csv/json/ndjson).

    Diferencia esencial con ``render_value``: acá el resultado **no es SQL**, así que no
    lleva comillas ni escapado de literal — el escapado que corresponde es el del formato
    (comillas de CSV, ``json.dumps`` de la cadena) y lo aplica el writer, que es quien
    conoce el separador y el dialecto de CSV.

    ``None`` devuelve la cadena VACÍA, que en un CSV es indistinguible de una cadena vacía
    real. **Distinguir NULL de '' es responsabilidad del llamador**, que es el único que
    sabe qué convención usa su formato (``json``/``ndjson`` emiten ``null`` nativo; un CSV
    necesita un centinela explícito). Comprobá ``value is None`` ANTES de llamar si tu
    formato necesita la distinción.

    Se conservan las dos reglas del módulo: ``Decimal`` como ``str`` (nunca float) y byte
    nulo / tipo desconocido ⇒ ``UnsupportedValueError``.
    """
    if binary_encoding not in BINARY_ENCODINGS:
        raise UnsupportedValueError(f"binary_encoding:{binary_encoding}")

    if value is None:
        return ""
    if isinstance(value, bool):
        # Minúsculas: es la grafía de JSON y la que cualquier importador de CSV entiende.
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise UnsupportedValueError("float no finito")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedValueError("decimal no finito")
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if binary_encoding == "base64":
            return base64.b64encode(raw).decode("ascii")
        return raw.hex()
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return format_timedelta(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        s = value
    else:
        raise UnsupportedValueError(type(value).__name__)
    if "\x00" in s:
        raise UnsupportedValueError("null_byte")
    return s


__all__ = [
    "BINARY_ENCODINGS",
    "UnsupportedValueError",
    "render_value",
    "render_value_text",
]
