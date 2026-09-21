"""
Vocabulario cerrado de errores de BLUEPRINTS (``DatabaseModel``).

Estos códigos viajan en ``public_context["code"]``, **nunca** en ``context``. La distinción
no es de estilo: ``context`` solo se expone en ``development``, así que en producción el
operador recibiría el mensaje sin poder clasificarlo ni elegir la salida.

Por qué existe el módulo: ``DatabaseModelController`` lanzaba sus excepciones con ``context=``
solamente, así que el 409 de su ``PATCH`` era inclasificable fuera de desarrollo. Dos de sus
errores tienen salidas DISTINTAS —uno se resuelve eligiendo otro nombre, el otro NO se
resuelve cambiando el payload— y sin código el cliente no puede distinguirlos.
"""

# --------------------------------------------------------------------------- #
# Códigos de error                                                             #
# --------------------------------------------------------------------------- #

#: Se intentó cambiar el ``slug`` de un blueprint que ya tiene bases gestionadas.
#:
#: El ``slug`` no es una etiqueta: ``migrations.version_table_name`` lo usa para nombrar la
#: tabla de versión de Alembic (``_gw_v_{slug}``) DENTRO de cada BD gestionada. Cambiarlo no
#: renombra nada en los motores, así que la contabilidad de TODAS esas bases queda huérfana:
#: el gateway pasa a leer una tabla que no existe, ``get_current_version`` devuelve ``None``,
#: ``compute_pending`` reporta la cadena ENTERA como pendiente y un ``apply`` la reaplica
#: desde la primera versión sobre bases que ya tenían el esquema.
#:
#: Trae ``current_slug``, ``requested_slug`` y ``managed_database_count``. Salidas: renombrar
#: el ``name`` (que es libre y no nombra ninguna tabla), o desasociar las bases primero.
CODE_SLUG_IN_USE = "database_model.slug_in_use"

#: Ya existe otro blueprint con ese ``name`` o ese ``slug`` — las dos columnas son únicas.
#: Se distingue de ``CODE_SLUG_IN_USE`` porque acá el CTA es "elegí otro valor", mientras que
#: allá el valor pedido es irrelevante: no se puede cambiar el slug, punto.
CODE_NAME_OR_SLUG_TAKEN = "database_model.name_or_slug_taken"

ERROR_CODES = frozenset(
    {
        CODE_SLUG_IN_USE,
        CODE_NAME_OR_SLUG_TAKEN,
    }
)
