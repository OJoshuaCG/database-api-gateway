#!/usr/bin/env python3
"""Guard del tamaño y la forma de CLAUDE.md.

POR QUÉ EXISTE
--------------
CLAUDE.md se carga COMPLETO en cada sesión de agente. Cada línea se paga en
todas las conversaciones, incluida la que solo venía a cambiar un endpoint.

En agosto de 2026 el archivo llegó a 2316 líneas (~40.000 tokens por sesión).
No lo hizo nadie de una vez: lo hicieron ~40 agregados, cada uno de los cuales,
mirado solo, estaba justificado — el post-mortem de un fix real, el gotcha de
un módulo nuevo, la causa raíz de un incidente de producción. Ese es el punto:
**la regla no se rompe con una decisión mala, se rompe con muchas razonables.**
Por eso el tope tiene que ser mecánico y no un criterio, y por eso el remedio
no es "sean prudentes" sino un chequeo que falle.

QUÉ VERIFICA
------------
1. Tope duro de líneas. Si tu agregado no entra, algo tiene que salir.
2. Ausencia de fechas. Una fecha es la firma inconfundible de una entrada de
   changelog, y un changelog en CLAUDE.md es contenido en el archivo
   equivocado: la historia va en git y en docs/development/.

Lo que NO verifica es si el contenido merece estar (eso es criterio humano,
y está escrito como las 7 reglas del propio CLAUDE.md).

Solo stdlib: sirve cualquier Python 3, sin venv ni .env.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_LINES = 250

# Fechas ISO (2026-08-14) y las formas es/en más comunes en los post-mortems
# que este guard existe para desalojar.
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b\d{1,2}\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
        r"septiembre|setiembre|octubre|noviembre|diciembre)\b",
        re.IGNORECASE,
    ),
)

# El propio bloque de reglas cita fechas como EJEMPLO de lo prohibido, y el
# encabezado nombra el incidente que originó el tope. Sin esta exención el
# guard se rechazaría a sí mismo.
_EXEMPT = re.compile(r"^\s*(\d+\.\s+)?\*\*Prohibidas las fechas|En agosto de 2026 llegó a")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    target = root / "CLAUDE.md"

    if not target.is_file():
        print("check_claude_md_size: no se encontró CLAUDE.md; nada que verificar.")
        return 0

    lines = target.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []

    if len(lines) > MAX_LINES:
        problems.append(
            f"CLAUDE.md tiene {len(lines)} líneas y el tope es {MAX_LINES}.\n"
            f"    Sobran {len(lines) - MAX_LINES}. No subas el tope: mové contenido.\n"
            f"    · El «cómo se usa» va a docs/features/<feature>.md\n"
            f"    · El «por qué» y la causa raíz van a\n"
            f"      docs/development/decisiones-e-incidentes.md\n"
            f"    · El porqué de un fix puntual va al docstring de su función"
        )

    dated = [
        (n, line.strip())
        for n, line in enumerate(lines, start=1)
        if not _EXEMPT.match(line) and any(p.search(line) for p in _DATE_PATTERNS)
    ]
    if dated:
        problems.append(
            f"CLAUDE.md tiene {len(dated)} línea(s) con fecha. Una fecha significa que\n"
            f"    estás escribiendo un changelog en el archivo equivocado:"
        )
        problems.extend(f"      L{n}: {text[:96]}" for n, text in dated[:10])
        if len(dated) > 10:
            problems.append(f"      … y {len(dated) - 10} más")

    if problems:
        print("check_claude_md_size: FALLA\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        return 1

    print(f"check_claude_md_size: OK ({len(lines)}/{MAX_LINES} líneas, sin fechas).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
