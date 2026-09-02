"""
Vocabulario cerrado de códigos de error del aprovisionamiento de BDs gestionadas.

Estos códigos viajan en ``public_context["code"]``, **nunca** en ``context``: ``context`` solo
se expone en ``development`` (ver ``HandlerExceptions``), así que en producción el operador
recibiría el mensaje sin poder clasificarlo, y la SPA no podría elegir el CTA de recuperación.
``public_context.code`` es exactamente lo que el cliente ya lee (``ApiError.code``).

Vive en un módulo aparte y no dentro de un controller porque lo comparten
``ManagedDatabaseController`` (que emite los cuatro primeros) y ``ManagedMigrationController``
(que emite ``CODE_NOT_PROVISIONED``); tenerlo en uno de los dos obligaría a un import cruzado
entre controllers solo por unas constantes.
"""

# --------------------------------------------------------------------------- #
# Códigos                                                                      #
# --------------------------------------------------------------------------- #

#: La BD ya existe FÍSICAMENTE en el motor, así que no hay nada que aprovisionar. La vía para
#: traerla al inventario sin recrearla es ``POST /managed-databases/adopt``.
CODE_EXISTS_IN_ENGINE = "managed_database.exists_in_engine"

#: El inventario ya la marca ``active``. Se puede forzar con ``allow_recreate=true`` cuando la
#: BD fue borrada por fuera del gateway.
CODE_ALREADY_ACTIVE = "managed_database.already_active"

#: La BD está ``archived``: retirada del uso. No se aprovisiona, y no hay escape.
CODE_ARCHIVED = "managed_database.archived"

#: La fila está en ``error`` pero la BD SÍ existe en el motor, o sea que el ``error`` es
#: cuarentena de migraciones y no un ``CREATE DATABASE`` fallido. Aprovisionar es la
#: herramienta equivocada: la salida es ``reconcile-partial`` o ``apply?force=true``.
CODE_QUARANTINED_NOT_MISSING = "managed_database.quarantined_not_missing"

#: Una operación de migraciones se pidió sobre una BD que no existe en el motor. Emitido por
#: ``apply``/``rollback``/``stamp``/``reconcile-partial``.
CODE_NOT_PROVISIONED = "managed_database.not_provisioned"

ERROR_CODES = frozenset(
    {
        CODE_EXISTS_IN_ENGINE,
        CODE_ALREADY_ACTIVE,
        CODE_ARCHIVED,
        CODE_QUARANTINED_NOT_MISSING,
        CODE_NOT_PROVISIONED,
    }
)

#: ``model_version`` ya no se acepta en el ALTA. Declararla escribía la caché del inventario sin
#: tocar el motor, así que la base quedaba vacía diciendo estar migrada — y esa caché alimenta
#: ``_policy_flags``, o sea que además congelaba la versión del blueprint como ``in_use`` sin que
#: ninguna base la tuviera aplicada. Es el mismo agujero que el ``PATCH`` ya había cerrado.
#: Reemplazos: ``apply_migrations``/``target_version`` para migrar de verdad al crear, y
#: ``POST /managed-databases/adopt`` para una base que YA está físicamente en esa versión.
CODE_MODEL_VERSION_NOT_WRITABLE = "managed_database.model_version_not_writable"

#: ``apply_migrations`` sin las condiciones que lo hacen posible: exige ``provision=true`` (no se
#: migra lo que no existe en el motor) y ``model_id`` (no hay migraciones sin blueprint).
CODE_APPLY_REQUIRES_PROVISION = "managed_database.apply_requires_provision"
