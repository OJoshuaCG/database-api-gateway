"""
Quién ve el CÓDIGO de vistas, rutinas, triggers y eventos (capacidad ``schema.definitions``).

Qué protege y qué no
--------------------
El snapshot de una base y las comparaciones de esquema devuelven, junto con la ESTRUCTURA, el
CUERPO de los objetos que tienen código: vistas, vistas materializadas, rutinas, triggers y
eventos. Ese texto es de un tercero (reglas de negocio, literales, a veces secretos) y se leía con
``databases.read`` / ``schema_diff.read``, o sea, con el rol ``viewer``. Ahora el cuerpo se entrega
solo a quien tiene ``schema.definitions`` EN el destino (``operator`` y ``owner`` por rol); al
resto, el objeto sale igual —nombre, tipo, dependencias— pero SIN cuerpo y con ``redacted=true``.

Las TABLAS no se redactan: su DDL es estructura (columnas, índices, claves). Secuencias, tipos y
extensiones tampoco: no tienen código.

Por qué una marca explícita y no un campo vacío
-----------------------------------------------
Un ``ddl`` vacío sin más significa «este objeto no tiene definición», que es mentira y rompe a
quien lo re-aplica sin mirar. ``redacted=true`` dice «existe y no se te muestra».

Quién queda SIN redactar a propósito
------------------------------------
Los consumidores INTERNOS (``ServerController.snapshot`` para crear un blueprint desde un
snapshot, los clones, las exportaciones) trabajan con el dump COMPLETO: la redacción se aplica
solo en la capa de respuesta de las rutas de lectura (``redact_dump`` y los helpers de ítems de
comparación), nunca en el controller que arma el dump.

Esto NO es una frontera completa del código de los objetos: un blueprint creado desde un snapshot
guarda los cuerpos en sus versiones, y esas se leen con ``blueprints.read`` (``viewer``). Ver el
archivo de decisiones e incidentes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.capability_catalog import Capability
from app.services.db_admin.dtos import DumpStatement, StructureDump

if TYPE_CHECKING:
    from app.core.actor import Actor
    from app.core.scope import ScopeTarget

#: Tipos de objeto cuyo CUERPO se redacta. Mismo vocabulario que ``DumpStatement.object_type`` y
#: que ``DiffItem.object_type`` de ``schema_diff``.
DEFINITION_OBJECT_TYPES: frozenset[str] = frozenset(
    {"view", "materialized_view", "routine", "trigger", "event"}
)


def is_definition_object(object_type: str) -> bool:
    """¿Este tipo de objeto tiene un cuerpo que se redacta?"""
    return object_type in DEFINITION_OBJECT_TYPES


def actor_reads_definitions(actor: "Actor | dict | None", target: "ScopeTarget") -> bool:
    """
    ¿El actor tiene ``schema.definitions`` EN ``target`` (capas 1 y 2)? Fail-closed: un actor que
    no es un ``Actor`` (``dict`` legado o ``None``) no lee cuerpos.

    No deja rastro de denegación (``can_at``): un «no» acá no es un intento denegado, es la
    decisión de qué mostrar.
    """
    from app.core.actor import Actor as ActorType
    from app.core.scope import can_at

    if not isinstance(actor, ActorType):
        return False
    return can_at(actor, Capability.SCHEMA_DEFINITIONS, target)


def redact_statement(statement: DumpStatement) -> DumpStatement:
    """El mismo objeto con el cuerpo vaciado y ``redacted=true``; las tablas pasan intactas."""
    if not is_definition_object(statement.object_type):
        return statement
    return statement.model_copy(update={"ddl": "", "redacted": True})


def redact_dump(dump: StructureDump) -> StructureDump:
    """El dump con el cuerpo de cada vista, rutina, trigger y evento vaciado y marcado."""
    return dump.model_copy(
        update={"statements": [redact_statement(statement) for statement in dump.statements]}
    )


def redact_item(item: dict) -> dict:
    """
    Un ítem de comparación (``sql``/``down_sql``) sin cuerpo y con ``redacted=true`` si es de un
    tipo con código; el resto sale igual, con ``redacted=false``. Devuelve una copia.

    ``down_sql`` ausente sigue ausente: el flag no inventa un rollback que no existía.
    """
    redacted = is_definition_object(item["object_type"])
    out = dict(item)
    out["redacted"] = redacted
    if redacted:
        out["sql"] = ""
        if out.get("down_sql") is not None:
            out["down_sql"] = ""
    return out


def mark_visible(item: dict) -> dict:
    """El ítem tal cual, con ``redacted=false`` (quien lee ve los cuerpos)."""
    out = dict(item)
    out["redacted"] = False
    return out
