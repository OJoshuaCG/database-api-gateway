"""
Registro de tools: inmutable, con sus invariantes afirmados AL IMPORTAR.

POR QUÉ LOS INVARIANTES SE AFIRMAN AL IMPORTAR
----------------------------------------------
Mismo criterio que el catálogo de capacidades: **fallar al importar es fallar al arrancar**, que
es lo correcto para un registro que decide qué puede pedir un agente. Un test que lo verifique
también existe, pero un test se puede saltear con un marker y el import no.

LO QUE EL ``tools/list`` PUBLICA ES SUPERFICIE DE ATAQUE
--------------------------------------------------------
La descripción de una tool la lee el modelo del agente y la trata como instrucción — es el vector
de *tool poisoning*. Así que las descripciones de acá son **descriptivas y cerradas**: dicen qué
hace la tool y qué devuelve, sin frases imperativas, sin "usá esto siempre que…", y sin nada que
pueda leerse como una orden que reordene las prioridades del agente.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """
    Una tool. ``touches_engine`` no es informativo: decide si hace falta el façade de solo
    lectura, y por lo tanto si la tool puede correr sin credencial de servidor.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable
    touches_engine: bool
    #: Scope que la tool exige. En v1 todas piden el mismo, y el campo existe igual para que
    #: agregar una tool con otro scope no sea un cambio de forma.
    scope: str = "blueprints.read"
    tags: tuple[str, ...] = field(default_factory=tuple)


def _spec(**kwargs) -> ToolSpec:
    return ToolSpec(**kwargs)


def _build() -> tuple[ToolSpec, ...]:
    from app.mcp.tools import inventory

    return (
        _spec(
            name="list_databases",
            description=(
                "Devuelve las bases de datos del proyecto de este token que están habilitadas "
                "para inspección, con su motor, entorno, blueprint y versión aplicada. Lee el "
                "inventario del gateway y no abre ninguna conexión a los motores."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=inventory.list_databases,
            touches_engine=False,
        ),
    )


TOOLS: tuple[ToolSpec, ...] = _build()
BY_NAME = {t.name: t for t in TOOLS}


def _assert_invariants() -> None:
    """
    Cuatro invariantes, y cada uno cierra un modo de fallo concreto.
    """
    nombres = [t.name for t in TOOLS]
    # 1. Nombres únicos: con dos iguales, `BY_NAME` se queda con el último en silencio y la
    #    tool que el agente cree estar llamando no es la que corre.
    assert len(nombres) == len(set(nombres)), f"tools duplicadas: {nombres}"

    for t in TOOLS:
        # 2. El schema de entrada es CERRADO. Sin `additionalProperties: false`, un agente
        #    puede mandar campos extra que el handler ignora — y "ignora" es donde vive la
        #    diferencia entre lo que el operador cree que pidió y lo que se ejecutó.
        assert t.input_schema.get("additionalProperties") is False, (
            f"{t.name}: el schema de entrada tiene que ser cerrado"
        )
        # 3. Ninguna descripción puede contener una instrucción. Es el vector de tool
        #    poisoning: el modelo lee esto como parte de su prompt.
        bajo = t.description.lower()
        for imperativo in ("ignorá", "ignora las", "siempre que", "debés", "tenés que"):
            assert imperativo not in bajo, (
                f"{t.name}: la descripción contiene una instrucción ({imperativo!r})"
            )
        # 4. El scope tiene que existir en el catálogo de capacidades y estar dentro del techo
        #    de agente. Un scope fuera del techo sería una tool que ningún token puede llamar.
        from app.services.capability_catalog import AGENT_ALLOWED, Capability

        cap = Capability(t.scope)
        assert cap in AGENT_ALLOWED, f"{t.name}: {t.scope} está fuera del techo de agente"


_assert_invariants()
