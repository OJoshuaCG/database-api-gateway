"""
Vocabulario cerrado del congelamiento de versiones de blueprint.

Una versión de ``model_migrations`` se congela (no se puede editar su SQL ni eliminarla)
mientras alguna BD gestionada dependa de ella. Estos códigos viajan en
``public_context["code"]``, **nunca** en ``context``: ``context`` solo se expone en
``development`` (ver ``HandlerExceptions``), así que en producción el operador recibiría el
mensaje sin poder clasificarlo ni elegir la salida.

Por qué existe el módulo: hasta ahora el guard consultaba solo ``database_migration_history``,
que es un LOG DE EVENTOS. Una fila ``status='applied'`` nunca se revoca —``_record_history`` se
llama igual desde el ``apply`` que desde el ``rollback``, y no hay columna ``direction``—, así
que una versión revertida correctamente en todas las BDs quedaba congelada de por vida sin
ninguna salida: no existe purga de historial, y el ``CASCADE`` que borraría esas filas cuelga
del borrado de la migración, que es justo lo bloqueado. Ahora el historial es el primer filtro
y la decisión la da la versión ACTUAL de esas BDs, con estos motivos por BD bloqueante.
"""

# --------------------------------------------------------------------------- #
# Códigos de error                                                             #
# --------------------------------------------------------------------------- #

#: Se intentó cambiar el SQL efectivo de una versión que alguna BD tiene aplicada HOY. Editarlo
#: no re-ejecuta nada en el motor, así que la metadata quedaría describiendo algo que no corrió.
#: Salida: fix-forward con una versión nueva, o revertir en las BDs que la nombran.
CODE_SQL_FROZEN = "model_migration.sql_frozen"

#: Se intentó eliminar una versión que alguna BD tiene aplicada HOY. Borrarla no toca el motor:
#: dejaría esa BD con objetos que ninguna versión del blueprint describe.
CODE_STILL_APPLIED = "model_migration.still_applied"

ERROR_CODES = frozenset({CODE_SQL_FROZEN, CODE_STILL_APPLIED})

# --------------------------------------------------------------------------- #
# Motivos por BD bloqueante (campo ``reason`` de ``blocking_databases``)        #
# --------------------------------------------------------------------------- #

#: El motor reporta que la BD está en esta versión o en una posterior. Es el caso normal.
REASON_STILL_APPLIED = "still_applied"

#: No se pudo leer la versión de esa BD (motor caído, base sin aprovisionar, credenciales
#: rotas). **Fail-closed**: cuenta como bloqueante. Tratar un fallo de lectura como "ya no la
#: tiene" convertiría una caída de red en permiso para borrar.
REASON_UNREADABLE = "unreadable"

#: Hay historial contra una BD que ya no está en el inventario. No debería ocurrir (el
#: ``ondelete='CASCADE'`` se lleva esas filas), pero si ocurre no se puede probar lo contrario.
REASON_UNKNOWN_DATABASE = "unknown_database"

#: El blueprint de la migración no se pudo resolver, así que no hay ``slug`` con el que ubicar
#: la tabla de versión ``_gw_v_{slug}`` dentro de cada BD.
REASON_UNKNOWN_BLUEPRINT = "unknown_blueprint"

BLOCKING_REASONS = frozenset(
    {
        REASON_STILL_APPLIED,
        REASON_UNREADABLE,
        REASON_UNKNOWN_DATABASE,
        REASON_UNKNOWN_BLUEPRINT,
    }
)
