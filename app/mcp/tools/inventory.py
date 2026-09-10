"""
``list_databases`` — el inventario alcanzable. **No toca ningún motor.**

Es la tool más barata y la primera que un agente llama: sin ella no tiene ningún
``database_id`` que pasarle a las demás, así que es la que convierte el resto en usable.

Que no abra conexiones no es un detalle de rendimiento: es lo que permite que un operador
verifique el gate —qué ve y qué no ve un token— **sin depender de que ningún motor esté
levantado**.
"""

from app.mcp.context import ToolContext


def list_databases(ctx: ToolContext, params: dict) -> dict:
    """
    Devuelve las bases habilitadas para este token.

    **Nunca enumera lo negado.** Un listado que dijera "y estas otras existen pero no te las
    doy" sería el mismo oráculo de inventario que el orden de los ejes del gate existe para
    evitar. El agente recibe lo que alcanza y no aprende nada de lo demás.

    **No devuelve ``server_id``.** El docstring anterior decía que "viaja porque dos bases del
    mismo servidor comparten motor" y el dict no lo incluía: el código estaba bien y el
    comentario mentía. No se incluye porque no hay ninguna tool que acepte un ``server_id`` — la
    referencia cruda (servidor + nombre) es la forma que se escapa del inventario, y en la v1 no
    se acepta. Un identificador que ninguna tool consume es superficie sin uso.
    """
    bases = ctx.reachable_databases()
    return {
        "databases": [
            {
                "database_id": b.database_id,
                "name": b.database,
                "engine": b.engine,
                "environment": b.environment_slug,
                "blueprint": b.model_slug,
                "applied_version": b.model_version,
            }
            for b in bases
        ],
        "count": len(bases),
        # Que la lista esté vacía es un resultado LEGÍTIMO y frecuente el primer día: el gate
        # niega por default, así que hasta que alguien habilite entorno y base no hay nada. Se
        # dice explícito para que el agente no lo lea como un fallo y reintente.
        "note": (
            "Vacío significa que ninguna base del proyecto tiene el opt-in de agentes todavía, "
            "no que haya fallado la consulta."
        ),
    }
