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
#:
#: HISTÓRICO: era el rechazo del criterio ``>=`` (la BD está en esta versión o en una
#: posterior). El borrado pasó a criterio de IGUALDAD y usa ``CODE_VERSION_IN_USE``; este
#: código sigue vigente para el resto de caminos que evalúan vigencia con ``>=``.
CODE_STILL_APPLIED = "model_migration.still_applied"

#: Se intentó eliminar una versión en la que alguna BD está PARADA exactamente. Es el único
#: caso que el borrado con renumerado no puede resolver: a las BDs que están adelante se les
#: mueve el puntero a la nueva etiqueta de su MISMA migración, pero una BD parada justo en la
#: versión que desaparece no tiene ninguna etiqueta nueva a la que apuntar.
CODE_VERSION_IN_USE = "model_migration.version_in_use"

#: No se pudo leer la versión de alguna BD del blueprint. **Fail-closed**: sin esa lectura no
#: se puede probar que la BD no esté parada en la versión a borrar, ni se la puede re-stampear
#: si está adelante. Proceder la dejaría huérfana (puntero a una revisión inexistente).
CODE_UNREADABLE_DATABASES = "model_migration.unreadable_databases"

#: El borrado requiere mover el puntero de una o más BDs (escritura REMOTA sobre cada motor),
#: y no llegó ``confirm_token``. Se obtiene del preview ``GET .../{version}/delete-plan``.
CODE_RENUMBER_CONFIRMATION_REQUIRED = "model_migration.renumber_confirmation_required"

#: El estado del parque cambió entre el preview y la ejecución (alguna BD se movió de versión).
#: El plan congelado ya no describe la realidad: hay que volver a pedir el preview.
CODE_RENUMBER_PLAN_STALE = "model_migration.renumber_plan_stale"

#: Falló el re-stamp de alguna BD. No se tocó el blueprint. El campo ``compensated`` dice si
#: las BDs ya movidas volvieron a su valor original; si es ``false``, quedaron a mitad y el
#: mensaje nombra BD y versión exacta de cada una.
CODE_RENUMBER_STAMP_FAILED = "model_migration.renumber_stamp_failed"

#: Una BD adelantada tendría que quedar en una etiqueta que NO existe en la cadena vigente.
#: Solo ocurre si el blueprint tiene un hueco justo debajo de la versión en la que está esa BD
#: (los huecos existen porque ``create_migration`` acepta una ``version`` explícita). El stamp
#: corre ANTES del renumerado —Alembic tiene que resolver el valor actual del puntero— así que
#: su destino tiene que existir ya. Salida: rellenar el hueco, o mover esa BD antes.
CODE_RENUMBER_TARGET_MISSING = "model_migration.renumber_target_missing"

#: Alguna de las migraciones AFECTADAS por el renumerado (la que se borra o cualquiera
#: posterior) tiene una aplicación parcial sin resolver. El renumerado cambia el ``version``,
#: que entra en el ``checksum``, y eso invalidaría el checkpoint de esa aplicación a medias.
CODE_AFFECTED_PARTIAL = "model_migration.affected_partial_application"

ERROR_CODES = frozenset(
    {
        CODE_SQL_FROZEN,
        CODE_STILL_APPLIED,
        CODE_VERSION_IN_USE,
        CODE_UNREADABLE_DATABASES,
        CODE_RENUMBER_CONFIRMATION_REQUIRED,
        CODE_RENUMBER_PLAN_STALE,
        CODE_RENUMBER_STAMP_FAILED,
        CODE_RENUMBER_TARGET_MISSING,
        CODE_AFFECTED_PARTIAL,
    }
)

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

#: El motor reporta que la BD está EXACTAMENTE en esta versión. Es el motivo del borrado con
#: renumerado, que sí tolera las BDs adelante (``REASON_AHEAD``) pero no esta.
REASON_IN_USE = "in_use"

#: El motor reporta que la BD está en una versión POSTERIOR. No bloquea el borrado: se le
#: mueve el puntero a la nueva etiqueta de la misma migración. Aparece en el plan del preview,
#: no en la lista de bloqueantes.
REASON_AHEAD = "ahead"

BLOCKING_REASONS = frozenset(
    {
        REASON_STILL_APPLIED,
        REASON_UNREADABLE,
        REASON_UNKNOWN_DATABASE,
        REASON_UNKNOWN_BLUEPRINT,
        REASON_IN_USE,
    }
)


# --------------------------------------------------------------------------- #
# Edición de una versión que YA está aplicada (levantamiento del freeze)        #
# --------------------------------------------------------------------------- #
# ``CODE_SQL_FROZEN`` sigue siendo la respuesta por defecto: el freeze no se abre solo. Lo
# que sigue describe la ÚNICA vía para atravesarlo, con doble factor y rastro permanente.
#
# Por qué existe la vía: el freeze supone que la salida siempre es fix-forward, y para un
# cambio de comportamiento eso es cierto. Pero hay correcciones cuyo valor está justamente
# en que las BDs NUEVAS no repitan el defecto — el caso testigo es un ``COLLATE`` hardcodeado
# en el DDL de las primeras versiones: describirlo con una versión correctiva al final de la
# cadena obliga a toda base nueva a crearse mal y convertirse después.
#
# Lo que la vía NO hace, y por eso el rastro es obligatorio: editar ``up_sql`` **no
# re-ejecuta nada**. Las BDs que ya aplicaron la versión conservan FÍSICAMENTE lo que corrió,
# así que su ``_gw_v_{slug}`` pasa a afirmar una versión cuyo texto ya no es el que se les
# aplicó. Esa divergencia es real y no se puede deshacer; lo único que se puede evitar es que
# quede en SILENCIO.

#: Se pidió editar el SQL de una versión aplicada con un ``confirm_version`` que no coincide
#: con la versión de la ruta (422). Es el factor de "identificar CONSCIENTEMENTE qué se toca":
#: el mismo criterio que ``confirm_target_name`` en el borrado de una BD.
CODE_EDIT_CONFIRM_MISMATCH = "model_migration.edit_confirm_mismatch"

#: Se intentó cambiar el SQL con una aplicación PARCIAL sin resolver (checkpoint de
#: sentencia incompleto). Trae ``incomplete_progress`` con la BD y cuántas sentencias
#: alcanzó a commitear. Salida: reintentar ``apply`` (retoma solo) o limpiar con
#: ``stamp?force=true``. NO es lo mismo que ``CODE_SQL_FROZEN`` y no lo abre el doble
#: factor: acá el problema no es la divergencia, es que un resume interpretaría los
#: índices del checkpoint contra un SQL distinto del que corrió.
CODE_PARTIAL_APPLICATION = "model_migration.partial_application"

#: Se cambió ``up_sql`` dejando overrides por motor que quedarían obsoletos. Trae
#: ``stale_overrides`` con los nombres de campo. Salida: reenviarlos corregidos o
#: limpiarlos con ``null`` en la MISMA llamada. Es evitable desde la UI, y debería serlo:
#: el formulario ya sabe qué overrides existen.
CODE_STALE_OVERRIDES = "model_migration.stale_overrides"

ERROR_CODES = frozenset(
    {
        CODE_SQL_FROZEN,
        CODE_STILL_APPLIED,
        CODE_EDIT_CONFIRM_MISMATCH,
        CODE_PARTIAL_APPLICATION,
        CODE_STALE_OVERRIDES,
    }
)

#: Operación del ``confirm_token`` para esta vía. El token se ata al hash del SQL PROPUESTO
#: (parámetro ``subject``), no solo a la versión: sin eso se podría pedir el preview de una
#: corrección inocua y mandar otra cosa en el PATCH, que es exactamente lo que la
#: confirmación debe impedir (mismo razonamiento que la consola SQL).
CONFIRM_OPERATION = "model_migration.edit_applied"

#: Acción de auditoría que deja constancia PERMANENTE de la divergencia. Se registra con
#: ``target_type=AUDIT_TARGET_TYPE`` y ``target_id`` = id de la MIGRACIÓN (no del blueprint),
#: que es lo que permite derivar la bandera por versión con una sola query en lote.
#:
#: Vive en ``audit_log`` y no en una columna nueva por dos motivos, en este orden: (1) es un
#: HECHO histórico —"esta versión se editó después de haberse aplicado"— y los hechos no se
#: revocan, así que un log es el lugar correcto (a diferencia de
#: ``database_migration_history``, que se usaba mal como si fuera un estado); (2) ``audit_log``
#: no cuelga de ``managed_databases``, así que el rastro sobrevive a que se den de baja las
#: BDs divergentes — una columna en ``model_migrations`` también, pero exigiría una migración
#: Alembic, y el head se comparte con trabajo en curso.
AUDIT_ACTION_EDITED_AFTER_APPLY = "migration.sql_edited_after_apply"

#: ``target_type`` de esa entrada. Se separa de ``"database_model"`` (el que usa
#: ``migration.update``) justamente para poder filtrar por migración sin arrastrar el resto
#: de las acciones del blueprint.
AUDIT_TARGET_TYPE = "model_migration"
