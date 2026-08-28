"""
Vocabulario cerrado de errores de PROYECTOS (agrupación de blueprints).

Estos códigos viajan en ``public_context["code"]``, **nunca** en ``context``. La distinción
no es de estilo: ``context`` solo se expone en ``development`` (ver ``HandlerExceptions``),
así que en producción un cliente que dependa de él recibe el mensaje sin poder clasificarlo.

Por qué existe el módulo: ``ProjectController`` nació usando ``context=`` en sus siete
excepciones, y con eso el 422 de vinculación era **inutilizable en producción**. Ese 422 es
todo-o-nada a propósito, y su valor entero está en poder decirle al operador CUÁLES ids no
existen — dato que viajaba en ``context`` y por lo tanto desaparecía fuera de desarrollo. La
UI quedaba mostrando "hay blueprints inexistentes" sin poder señalar ninguno.
"""

# --------------------------------------------------------------------------- #
# Códigos de error                                                             #
# --------------------------------------------------------------------------- #

#: El proyecto no existe (o se borró entre dos llamadas).
CODE_NOT_FOUND = "project.not_found"

#: Hay ids de blueprint inexistentes en la selección a vincular. **No se vinculó ninguno.**
#: Trae ``missing_model_ids``, que es lo que la UI necesita para señalar las filas malas en
#: vez de invalidar la selección entera.
CODE_BLUEPRINTS_NOT_FOUND = "project.blueprints_not_found"

#: Ya existe otro proyecto con ese nombre (el nombre es único). Aplica al alta y al PATCH.
CODE_NAME_TAKEN = "project.name_taken"

#: Dos vinculaciones simultáneas chocaron en la PK compuesta del pivote. Es reintentable:
#: se distingue de ``CODE_NAME_TAKEN`` —que también es 409— porque el CTA es "reintentar",
#: no "elegí otro nombre".
CODE_LINK_CONFLICT = "project.link_conflict"

#: Se pidió desvincular un blueprint que no pertenece a ese proyecto. El blueprint puede
#: existir perfectamente: lo que no existe es el VÍNCULO.
CODE_BLUEPRINT_NOT_LINKED = "project.blueprint_not_linked"

#: El blueprint no existe. Se distingue de ``CODE_BLUEPRINT_NOT_LINKED`` porque el primero
#: es un 404 del recurso y el segundo un 404 de la relación, y el CTA difiere.
CODE_BLUEPRINT_NOT_FOUND = "project.blueprint_not_found"

ERROR_CODES = frozenset(
    {
        CODE_NOT_FOUND,
        CODE_BLUEPRINTS_NOT_FOUND,
        CODE_NAME_TAKEN,
        CODE_LINK_CONFLICT,
        CODE_BLUEPRINT_NOT_LINKED,
        CODE_BLUEPRINT_NOT_FOUND,
    }
)
