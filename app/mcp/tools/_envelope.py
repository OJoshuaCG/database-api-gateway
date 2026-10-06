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
from collections.abc import Callable
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

    def code_body(self, value, path: str, redact: Callable[[str], str] | None = None):
        """
        Código (vista, trigger, event, rutina) de un tercero: redactado, saneado, anotado y NUNCA
        capado.

        ``redact`` (opcional) enmascara credenciales y se aplica ANTES y DESPUÉS de sacar los
        caracteres de control. Antes, porque es lo que ve el patrón sobre el texto tal cual llegó.
        Después, porque quitar un carácter de control UNE lo que él separaba: un secreto (o la
        palabra clave que lo delata, ``IDENT\\x01IFIED BY``) partido por un control no matchea en
        la primera pasada y quedaría legible tras el saneado. La segunda pasada ve el texto que de
        verdad se entrega. El paquete no puede importar la capa de servicios, así que el
        redactor llega por parámetro (``ToolContext.redact_text``).

        Es la regla de las expresiones estructurales llevada al cuerpo entero: un cuerpo cortado a
        mitad es peor que ausente, porque el agente razonaría sobre código incompleto creyéndolo
        entero. El tope vive aguas arriba (``definition_reader``: sobre ``MAX_DEFINITION_BYTES`` el
        objeto vuelve como ``too_large``), así que acá no hay nada que recortar. Un cuerpo vacío no
        se anota: no hay texto de terceros que marcar.
        """
        if value is not None and redact is not None:
            value = redact(str(value))
        texto = clean(value)
        if texto is not None and texto != "" and redact is not None:
            texto = redact(texto)
        if texto is None or texto == "":
            return texto
        self.untrusted.append(path)
        return texto


def fingerprint(payload: dict) -> str:
    """sha256 corto del DTO de identidad ya normalizado: estable entre llamadas, para cachés."""
    crudo = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def iso(value) -> str | None:
    return value.isoformat() if value is not None else None
