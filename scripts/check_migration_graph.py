#!/usr/bin/env python3
"""Verifica que el grafo de migraciones Alembic del gateway sea aplicable.

Existe por un incidente de producción concreto (2026-08-22): dos ramas crearon una
migración cada una colgada del MISMO padre ``c7d8e9f0a1b2``. Git mergeó los dos archivos
**sin conflicto** —son archivos distintos que nunca se tocan— pero el DAG de Alembic vive
DENTRO del campo ``down_revision``, así que el árbol quedó con dos puntas. ``alembic upgrade
head`` es una RESOLUCIÓN DE NOMBRE: con dos candidatos no puede elegir, aborta antes de
abrir transacción, y el ``set -euo pipefail`` del ``entrypoint.sh`` mata el contenedor. El
gateway quedó en loop de reinicios, con la BD intacta pero sin arrancar.

Lo que hace peligroso a ese modo de fallo es que **es invisible antes del deploy**: no lo ve
el linter, no lo ve el compilador, no lo ve quien revisa el PR (cada migración es correcta
por separado) y sobre todo **no lo ve git**. El único momento en que aparece es al arrancar
en producción. Este guard mueve esa detección al push.

Chequeos, en orden:

1. **IDs de revisión duplicados.** Este repo elige los ``revision`` a mano y con forma
   secuencial (``d3e4f5a6b7c8``, ``d8e9f0a1b2c3``), no con el hash aleatorio de
   ``alembic revision``. Dos personas trabajando el mismo día eligen plausiblemente el
   mismo, y ahí el daño es PEOR que una bifurcación: una de las dos migraciones queda
   inalcanzable y su DDL nunca se aplica, sin que nada falle.
2. **``down_revision`` que apunta a la nada.** Un rebase o cherry-pick que dejó atrás la
   migración padre. Tampoco se ve en el diff.
3. **Head único.** El que causó el incidente.

POR QUÉ NO USA ``alembic.script.ScriptDirectory``
--------------------------------------------------
Sería lo natural —usar el parser de la herramienta que se está protegiendo— y la primera
versión de este script lo hacía. Se descartó por un problema MEDIDO, no teórico:
``ScriptDirectory`` **importa** cada archivo de migración, así que su veredicto sale del
``__pycache__`` cuando el bytecode está rancio. Reproducido: se editó un ``down_revision`` a
un valor de la MISMA longitud y se restauró dentro del mismo segundo; la invalidación de
bytecode de CPython compara mtime (granularidad de 1 s) y tamaño, los dos coincidieron, y
Alembic siguió dictaminando sobre una cadena que ya no estaba en el archivo.

Para un guard eso es inaceptable, y no por prolijidad: falla en la dirección PELIGROSA
—aprobar un árbol roto— y lo hace justo después de un rebase, un ``git checkout`` o un
cambio de rama, que son exactamente las operaciones que producen heads bifurcados. Un hook
de pre-push corre siempre en ese contexto.

Por eso se lee el **fuente** con ``ast``: nunca ejecuta ni importa nada, no puede leer un
``.pyc``, no necesita base de datos ni ``.env`` (y por lo tanto corre igual en un hook y en
CI sin credenciales), y ``revision``/``down_revision`` son literales a nivel de módulo en
los 26 archivos del repo, así que no hay nada que evaluar.

Uso:

    python scripts/check_migration_graph.py          # 0 = sano, 1 = roto
"""

from __future__ import annotations

import ast
import collections
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"


def _fail(message: str) -> int:
    print(f"\nFALLA: {message}\n", file=sys.stderr)
    return 1


def _literal(node: ast.AST) -> str | tuple[str, ...] | None:
    """Devuelve el valor de un literal ``str``/``None``/tupla-de-``str``.

    Cualquier otra cosa devuelve ``None`` y se trata como "no declarado": este script no
    evalúa expresiones a propósito, y en los 26 archivos del repo estos campos siempre son
    literales. La forma de tupla se acepta porque es la que emite ``alembic merge heads``.
    """
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


def _parse_migration(path: pathlib.Path) -> tuple[str | None, tuple[str, ...]]:
    """``(revision, padres)`` leídos del fuente, sin importar el módulo."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, str | tuple[str, ...] | None] = {}
    for node in tree.body:  # solo nivel de módulo: es donde Alembic los espera
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                found[target.id] = _literal(node.value)

    revision = found.get("revision")
    revision = revision if isinstance(revision, str) else None

    down = found.get("down_revision")
    if down is None:
        parents: tuple[str, ...] = ()
    elif isinstance(down, str):
        parents = (down,)
    else:
        parents = down
    return revision, parents


def main() -> int:
    if not VERSIONS_DIR.is_dir():
        return _fail(f"no existe {VERSIONS_DIR}. ¿Se está corriendo desde el repo?")

    by_revision: dict[str, list[str]] = collections.defaultdict(list)
    parents_of: dict[str, tuple[str, ...]] = {}
    unparsed: list[str] = []

    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        revision, parents = _parse_migration(path)
        if revision is None:
            unparsed.append(path.name)
            continue
        by_revision[revision].append(path.name)
        parents_of[revision] = parents

    if unparsed:
        return _fail(
            "estos archivos de alembic/versions/ no declaran un 'revision' literal a nivel\n"
            "de módulo, así que no se puede verificar el grafo:\n\n  - "
            + "\n  - ".join(unparsed)
        )

    if not by_revision:
        return _fail(
            f"no se encontró ninguna migración en {VERSIONS_DIR}.\n"
            "¿'script_location' está bien en alembic.ini?"
        )

    # ---- 1. IDs duplicados ---------------------------------------------------------- #
    duplicates = {rev: files for rev, files in by_revision.items() if len(files) > 1}
    if duplicates:
        detail = "\n".join(
            f"  - {rev}\n      " + "\n      ".join(files)
            for rev, files in sorted(duplicates.items())
        )
        return _fail(
            "hay IDs de revisión DUPLICADOS. Una de las migraciones que comparten ID queda\n"
            "inalcanzable y su DDL nunca se aplica, sin que nada falle:\n\n"
            f"{detail}\n\n"
            "Cómo arreglarlo: cambiá el 'revision' de la más nueva por un ID no usado (y su\n"
            "nombre de archivo), y actualizá el 'down_revision' de quien la referenciaba."
        )

    # ---- 2. Padres inexistentes ----------------------------------------------------- #
    dangling = [
        (rev, parent)
        for rev, parents in sorted(parents_of.items())
        for parent in parents
        if parent not in by_revision
    ]
    if dangling:
        detail = "\n".join(
            f"  - {rev} ({by_revision[rev][0]})\n      apunta a {parent}, que no existe"
            for rev, parent in dangling
        )
        return _fail(
            "hay migraciones cuyo 'down_revision' apunta a una revisión que no está en el\n"
            "repo, típicamente un rebase o cherry-pick que dejó atrás la padre. Alembic no\n"
            "va a poder construir la cadena:\n\n"
            f"{detail}\n\n"
            "Cómo arreglarlo: recuperá la migración faltante, o re-apuntá el 'down_revision'\n"
            "a la revisión que de verdad quedó como su padre en esta rama."
        )

    # ---- 3. Head único -------------------------------------------------------------- #
    referenced = {parent for parents in parents_of.values() for parent in parents}
    heads = sorted(set(by_revision) - referenced)

    if len(heads) > 1:
        detail = "\n".join(
            f"  - {head}  (cuelga de {parents_of[head][0] if parents_of[head] else '(base)'})"
            f"\n      {by_revision[head][0]}"
            for head in heads
        )
        return _fail(
            f"el árbol de migraciones tiene {len(heads)} heads y 'alembic upgrade head' no\n"
            "puede resolver a cuál ir. El contenedor NO va a arrancar:\n\n"
            f"{detail}\n\n"
            "Esto pasa cuando dos ramas crean una migración colgada del mismo padre. Git no\n"
            "lo marca como conflicto porque son archivos distintos.\n\n"
            "Cómo arreglarlo — ENCADENAR, no mergear:\n"
            "  1. Elegí cuál va primero. Si las dos migraciones son disjuntas (no comparten\n"
            "     tabla) el orden es indiferente; si NO lo son, va primero la que deja el\n"
            "     esquema en el estado que la otra asume.\n"
            "  2. En el archivo de la SEGUNDA, apuntá su 'down_revision' al 'revision' de la\n"
            "     primera, y actualizá el 'Revises:' del docstring para que no mienta.\n"
            "  3. Volvé a correr este script: tiene que dar un solo head.\n\n"
            "'alembic merge heads' también deja un head único, pero agrega una revisión vacía\n"
            "y CONSERVA la bifurcación en la historia. Encadenar deja la historia lineal, que\n"
            "es lo que un despliegue directo desde main necesita para ser predecible."
        )

    if not heads:
        return _fail(
            "no hay ningún head: todas las revisiones están referenciadas como padre, así que\n"
            "el grafo tiene un CICLO. Revisá los 'down_revision' recién tocados."
        )

    print(f"OK: grafo de migraciones sano — {len(by_revision)} revisiones, head único {heads[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
