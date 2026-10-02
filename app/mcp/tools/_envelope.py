"""
Saneamiento de texto y armado del envelope de confianza (plan 12 §6.3–§6.5).

DOS CLASES DE TEXTO, DOS REGLAS
-------------------------------
- **Texto LIBRE** (``COMMENT`` de tabla y de columna): se sanea, se capa a
  ``FREE_TEXT_MAX_CHARS`` y se anota en ``untrusted_fields``; si se recortó, también en
  ``clipped_fields``. Es el campo de mayor valor (dice qué significa cada columna) y de mayor
  riesgo (lo escribió un tercero).
- **Expresiones ESTRUCTURALES** (``CHECK``, columnas generadas, ``DEFAULT``, predicados de
  índices): se sanean y **nunca se capan**. Un ``CHECK`` cortado a la mitad es peor que ausente:
  el modelo asume una invariante que no existe. Si no entran, la respuesta falla por el
  presupuesto de bytes del dispatcher, que es el camino diseñado. Nunca se trunca.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

FREE_TEXT_MAX_CHARS = 512

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(value):
    """Caracteres de control fuera (salvo ``\\n`` y ``\\t``) y finales de línea normalizados."""
    if value is None:
        return None
    texto = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", texto)


class Tracker:
    """Acumula las rutas JSON de lo no confiable y de lo recortado mientras se arma la salida."""

    def __init__(self) -> None:
        self.untrusted: list[str] = []
        self.clipped: list[str] = []

    def free_text(self, value, path: str):
        """Texto libre de un tercero: saneado, capado y anotado."""
        texto = clean(value)
        if texto is None or texto == "":
            return texto
        self.untrusted.append(path)
        if len(texto) > FREE_TEXT_MAX_CHARS:
            self.clipped.append(path)
            return texto[:FREE_TEXT_MAX_CHARS]
        return texto


def fingerprint(payload: dict) -> str:
    """sha256 corto del DTO de identidad ya normalizado: estable entre llamadas, para cachés."""
    crudo = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def iso(value) -> str | None:
    return value.isoformat() if value is not None else None
