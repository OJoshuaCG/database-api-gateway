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

# --------------------------------------------------------------------------- #
# Renombrado del slug propagado a los motores                                  #
# --------------------------------------------------------------------------- #
# El ``slug`` SÍ se puede cambiar, pero no por el ``PATCH`` común: cambiarlo renombra una
# tabla DENTRO de cada BD gestionada, o sea N escrituras remotas sobre bases de terceros sin
# transacción compartida. Por eso tiene endpoint propio, preview y doble factor — el mismo
# molde que el borrado de una versión con renumerado.

#: Alguna BD ya tiene una tabla con el nombre DESTINO (``_gw_v_{slug_nuevo}``). Renombrar
#: encima la pisaría o fallaría según el motor, y esa tabla puede ser el puntero bueno de otro
#: blueprint —o el residuo de un rename anterior que quedó a medias—. **Bloquea todo el
#: renombrado**, no solo esa BD: dejar la mitad del parque renombrada es el estado del que
#: cuesta salir. Trae ``conflicting_databases``.
CODE_SLUG_RENAME_CONFLICT = "database_model.slug_rename_conflict"

#: No se pudo leer alguna BD del blueprint. **Fail-closed**: no se puede probar que no tenga
#: la tabla, y renombrar el resto la dejaría huérfana sin que nada falle. Trae
#: ``unreachable_databases``.
CODE_SLUG_RENAME_UNREACHABLE = "database_model.slug_rename_unreachable"

#: El renombrado implica tocar motores y no llegó ``confirm_token``. Se obtiene del preview.
#: Trae ``rename_plan`` para que el cliente pueda mostrar QUÉ bases se van a tocar.
CODE_SLUG_RENAME_CONFIRMATION_REQUIRED = "database_model.slug_rename_confirmation_required"

#: El token no corresponde a la huella del parque que congeló el preview: alguna BD cambió de
#: estado en el medio. Salida: volver a pedir el plan.
CODE_SLUG_RENAME_PLAN_STALE = "database_model.slug_rename_plan_stale"

#: Falló el rename en alguna BD. Trae ``renamed`` (las que sí), ``failed`` y ``not_compensated``
#: —las que quedaron con el nombre NUEVO y no se pudieron devolver—. El ``slug`` del gateway
#: **no se tocó**: se actualiza último y solo si todas las bases respondieron bien.
CODE_SLUG_RENAME_FAILED = "database_model.slug_rename_failed"

ERROR_CODES = frozenset(
    {
        CODE_SLUG_IN_USE,
        CODE_NAME_OR_SLUG_TAKEN,
        CODE_SLUG_RENAME_CONFLICT,
        CODE_SLUG_RENAME_UNREACHABLE,
        CODE_SLUG_RENAME_CONFIRMATION_REQUIRED,
        CODE_SLUG_RENAME_PLAN_STALE,
        CODE_SLUG_RENAME_FAILED,
    }
)

#: Operación del ``confirm_token`` de este renombrado. Se reusa el servicio con
#: ``server_id=model_id`` y ``db_name=slug_viejo:slug_nuevo`` (mismo criterio que el borrado
#: con renumerado, que usa ``server_id=model_id`` y ``db_name=slug:version``).
RENAME_SLUG_OPERATION = "database_model.rename_slug"
