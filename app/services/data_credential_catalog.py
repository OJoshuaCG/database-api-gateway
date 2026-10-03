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

ERROR_CODES = frozenset(
    {
        CODE_DATA_CREDENTIAL_MISSING,
        CODE_DATA_PROBE_FAILED,
        CODE_DATA_ACCOUNT_ALREADY_EXISTS,
        CODE_DATA_PROVISION_IN_PROGRESS,
        CODE_DATA_DATABASE_NOT_ELIGIBLE,
    }
)
