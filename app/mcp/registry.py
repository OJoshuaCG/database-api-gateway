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

NINGUNA TOOL MUTA, Y ESA ES LA GARANTÍA QUE SOSTIENE TODO LO DEMÁS
-----------------------------------------------------------------
El envelope de confianza (``notice``, ``untrusted_fields``) es una mitigación de eficacia
desconocida contra inyección de prompt. El control REAL es que no existe ninguna tool que
escriba: una inyección exitosa no consigue ninguna acción. **El día que se agregue una tool
mutante esa garantía cae entera** y el análisis del plan 12 §6.4 hay que reabrirlo antes de
mergearla, no después.

``tools/list`` publica solo las tools cuyo scope tiene el token (``tools_for``): lo que un token
no puede llamar no es superficie que necesite ver.
"""

from dataclasses import dataclass, field
from typing import Callable


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


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
    #: Pistas de la spec MCP para el cliente (``tools/list``). Ninguna tool muta, así que todas
    #: son de solo lectura e idempotentes; el invariante 5 lo afirma al importar.
    annotations: dict = field(default_factory=lambda: dict(READ_ONLY_ANNOTATIONS))


def _spec(**kwargs) -> ToolSpec:
    return ToolSpec(**kwargs)


_DATABASE_ID = {
    "type": "integer",
    "minimum": 1,
    "description": "El database_id que devuelve list_databases.",
}
_KIND = {"type": "string", "enum": ["table", "view", "routine", "trigger", "sequence"]}


def _build() -> tuple[ToolSpec, ...]:
    from app.core.environments import MCP_MAX_OBJECTS_PER_CALL
    from app.mcp.tools import catalog, inventory, operations, search

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
        _spec(
            name="list_objects",
            description=(
                "Devuelve el índice de objetos de una base (tablas, vistas, rutinas, triggers y "
                "secuencias) con su tipo y nombre, sin estructura. Lee el catálogo del motor con "
                "una credencial de solo lectura. Los cuerpos de vistas, rutinas y triggers no se "
                "incluyen."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": _DATABASE_ID,
                    "kinds": {"type": "array", "items": _KIND, "minItems": 1},
                    "name_prefix": {"type": "string", "maxLength": 128},
                    "include_column_counts": {"type": "boolean", "default": False},
                },
                "required": ["database_id"],
                "additionalProperties": False,
            },
            handler=catalog.list_objects,
            touches_engine=True,
            scope="databases.read",
        ),
        _spec(
            name="check_freshness",
            description=(
                "Devuelve la versión de esquema que una base declara, si el gateway puede probar "
                "que esa versión se aplicó y si hay una aplicación a medias. No devuelve "
                "estructura ni datos: sirve para saber si una lectura anterior de get_schema "
                "quedó vieja."
            ),
            input_schema={
                "type": "object",
                "properties": {"database_id": _DATABASE_ID},
                "required": ["database_id"],
                "additionalProperties": False,
            },
            handler=catalog.check_freshness,
            touches_engine=True,
            scope="databases.read",
        ),
        _spec(
            name="get_schema",
            description=(
                "Devuelve la estructura de los objetos indicados de una base: columnas, tipos, "
                "claves, índices, claves foráneas y restricciones. Los objetos que no existen "
                "vuelven en 'missing'. Los comentarios son texto de terceros y se listan en "
                "'untrusted_fields'. No incluye cuerpos de vistas, rutinas ni triggers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": _DATABASE_ID,
                    "objects": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MCP_MAX_OBJECTS_PER_CALL,
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": _KIND,
                                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                            },
                            "required": ["kind", "name"],
                            "additionalProperties": False,
                        },
                    },
                    "include_indexes": {"type": "boolean", "default": True},
                    "include_foreign_keys": {"type": "boolean", "default": True},
                },
                "required": ["database_id", "objects"],
                "additionalProperties": False,
            },
            handler=catalog.get_schema,
            touches_engine=True,
            scope="databases.read",
        ),
        _spec(
            name="search_schema",
            description=(
                "Busca en la ESTRUCTURA de una base —nombres de tablas y vistas, nombres de "
                "columnas y comentarios de tablas y columnas— cuando no se conoce el nombre "
                "exacto. No distingue mayúsculas ni acentos, separa snake_case y camelCase, y pide que "
                "estén todas las palabras de la consulta. Devuelve por resultado el tipo, la "
                "tabla, la columna, el tipo de dato, las claves, el comentario, el puntaje y el "
                "campo que coincidió, con el objeto a pasar a get_schema. Nunca lee filas. Los "
                "comentarios son texto de terceros y se listan en 'untrusted_fields'. Si el tope "
                "de lectura recorta la búsqueda, 'truncated' vale true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": _DATABASE_ID,
                    "query": {"type": "string", "minLength": 2, "maxLength": 100},
                    "kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["table", "view", "column", "routine", "trigger"],
                        },
                        "minItems": 1,
                        "maxItems": 5,
                        "uniqueItems": True,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                "required": ["database_id", "query"],
                "additionalProperties": False,
            },
            handler=search.search_schema,
            touches_engine=True,
            scope="databases.read",
        ),
        _spec(
            name="diff_schemas",
            description=(
                "Compara la estructura de dos bases y devuelve la lista de diferencias: tipo de "
                "objeto, nombre, tipo de cambio y atributos que difieren. No genera SQL ni "
                "devuelve cuerpos o valores por defecto, y no guarda la comparación."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_database_id": _DATABASE_ID,
                    "target_database_id": _DATABASE_ID,
                },
                "required": ["source_database_id", "target_database_id"],
                "additionalProperties": False,
            },
            handler=catalog.diff_schemas,
            touches_engine=True,
            scope="schema_diff.read",
        ),
        _spec(
            name="list_environments",
            description=(
                "Devuelve los entornos de las bases que este token alcanza, con su política "
                "(si admite agentes y si bloquea migraciones destructivas) y cuántas de esas "
                "bases hay en cada uno. Lee el inventario del gateway y no abre conexiones."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=operations.list_environments,
            touches_engine=False,
            scope="environments.read",
        ),
        _spec(
            name="list_exports",
            description=(
                "Devuelve el estado de los trabajos de exportación de las bases que este token "
                "alcanza: estado, fase y fechas. No incluye el contenido, los archivos ni la "
                "forma de descargarlos."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=operations.list_exports,
            touches_engine=False,
            scope="exports.read",
        ),
        _spec(
            name="list_clones",
            description=(
                "Devuelve el estado de los trabajos de clonado en los que participa alguna base "
                "que este token alcanza: estado, fase y fechas. Un lado del clonado que el token "
                "no alcanza aparece sin identificar."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=operations.list_clones,
            touches_engine=False,
            scope="clones.read",
        ),
        _spec(
            name="list_catalogs",
            description=(
                "Devuelve los catálogos de referencia del gateway: privilegios por motor, charsets "
                "y collations habilitados, y las plantillas de perfiles de permisos (nivel y "
                "privilegios). No describe ninguna base, servidor ni usuario del motor."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=operations.list_catalogs,
            touches_engine=False,
            scope="catalogs.read",
        ),
    )


TOOLS: tuple[ToolSpec, ...] = _build()
BY_NAME = {t.name: t for t in TOOLS}


def tools_for(actor) -> list[ToolSpec]:
    """Las tools que ``actor`` puede llamar: las de ``TOOLS`` cuyo scope está en sus capacidades."""
    from app.services.capability_catalog import Capability

    return [t for t in TOOLS if actor.has(Capability(t.scope))]


def _assert_invariants() -> None:
    """
    Cinco invariantes, y cada uno cierra un modo de fallo concreto.
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
        # 5. Ninguna tool se declara mutante: las pistas publicadas tienen que decir solo lectura.
        assert t.annotations.get("readOnlyHint") is True, (
            f"{t.name}: readOnlyHint tiene que ser true"
        )
        assert t.annotations.get("destructiveHint") is False, (
            f"{t.name}: destructiveHint tiene que ser false"
        )


_assert_invariants()
