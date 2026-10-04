"""
Vocabulario cerrado de códigos de error de la credencial de DATOS por base gestionada.

Viajan en ``public_context["code"]``, nunca en ``context`` (solo visible en ``development``).
Los emite ``ManagedDatabaseController.provision_data_credential`` / ``clear_data_credential``.
"""

#: ``POST /managed-databases/{id}/data-credential/provision`` encontró en el motor una cuenta con
#: el usuario de datos (``MCP_DATA_ACCOUNT_PREFIX`` + id) que NO es del gateway (esta base no
#: guarda esa credencial). Rotarla le rompería la app a un tercero, así que se rechaza ANTES de
#: mutar nada. 409.
CODE_DATA_ACCOUNT_ALREADY_EXISTS = "data_credential.account_already_exists"

#: Ya hay un aprovisionamiento o una revocación de la credencial de datos de ESTA base en curso
#: (lock por base, solo dentro del proceso). 409, no se cambió nada: reintentar al terminar.
CODE_DATA_PROVISION_IN_PROGRESS = "data_credential.provision_in_progress"

#: La base no admite credencial de datos: no está ``active`` (no existe físicamente en el motor)
#: o es una base de sistema o la propia base de metadatos del gateway. 409, antes de mutar.
CODE_DATA_DATABASE_NOT_ELIGIBLE = "data_credential.database_not_eligible"

#: La base no tiene credencial de datos que verificar (nunca se aprovisionó, o se revocó). 409,
#: no es un fallo del motor: falta el paso de aprovisionar.
CODE_DATA_CREDENTIAL_MISSING = "data_credential.missing"

#: La sonda de la credencial de datos observó que NO es "SELECT sobre exactamente esta base": puede
#: escribir, ve más de una base o hay tablas que reenvían a otro servidor. 422. La verificación
#: queda borrada (``verified_at`` = null): las tools de datos la rechazan hasta que pase.
#: ``public_context.reasons`` lleva los códigos públicos (``CREDENTIAL_TOO_BROAD``,
#: ``WRITE_PRIVILEGE_PRESENT``, ``FEDERATED_TABLE_PRESENT``, ``PROBE_NOT_GREEN``) y
#: ``public_context.violations`` los motivos cortos; nunca el texto de un grant.
CODE_DATA_PROBE_FAILED = "managed_database.data_probe_failed"

#: Opt-in de datos (``request/approve/revoke_data_access``). El solicitante NO puede aprobar su
#: propio pedido: en los entornos que lo exigen (por defecto ``production``) lo aprueba OTRO owner.
#: 403, no se cambió nada.
CODE_DATA_ACCESS_SELF_APPROVAL = "data_access.self_approval_forbidden"

#: ``approve`` sin un pedido pendiente (nunca se pidió, ya está abierto, o se revocó). 409.
CODE_DATA_ACCESS_NOT_PENDING = "data_access.not_pending"

#: ``request`` sobre una base cuyo acceso a datos YA está abierto. 409: revocá primero si querés
#: re-pedirlo. Evita que un pedido repetido pise al aprobador original.
CODE_DATA_ACCESS_ALREADY_OPEN = "data_access.already_open"

#: La identidad del actor no es un usuario del gateway (llamada interna o dict legado): el
#: solicitante/aprobador tiene que quedar registrado, así que se rechaza. 403, fail-closed.
CODE_DATA_ACCESS_IDENTITY_REQUIRED = "data_access.identity_required"

ERROR_CODES = frozenset(
    {
        CODE_DATA_ACCESS_SELF_APPROVAL,
        CODE_DATA_ACCESS_NOT_PENDING,
        CODE_DATA_ACCESS_ALREADY_OPEN,
        CODE_DATA_ACCESS_IDENTITY_REQUIRED,
        CODE_DATA_CREDENTIAL_MISSING,
        CODE_DATA_PROBE_FAILED,
        CODE_DATA_ACCOUNT_ALREADY_EXISTS,
        CODE_DATA_PROVISION_IN_PROGRESS,
        CODE_DATA_DATABASE_NOT_ELIGIBLE,
    }
)
