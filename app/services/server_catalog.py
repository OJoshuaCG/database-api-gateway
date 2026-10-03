"""
Vocabulario cerrado de códigos de error del inventario de SERVIDORES.

Viajan en ``public_context["code"]``, nunca en ``context`` (solo visible en ``development``).
Los emite ``ServerController``.
"""

#: El ``PATCH`` re-apunta el servidor (cambia ``host``, ``port`` o ``engine``) o debilita su TLS
#: (desde ``require``/``verify-ca``/``verify-full`` hacia un modo más débil) sin traer
#: ``root_password``. La credencial guardada no se reutiliza contra un destino distinto del
#: que se registró: hay que volver a enviarla en el mismo request. ``public_context.fields``
#: lista qué campos dispararon la exigencia.
CODE_CREDENTIAL_REQUIRED_FOR_REBIND = "server.credential_required_for_rebind"

#: ``test-connection?credential=readonly`` sobre un servidor sin credencial de solo lectura
#: registrada. 409: no es un fallo del motor, falta un dato del inventario.
CODE_READONLY_CREDENTIAL_MISSING = "server.readonly_credential_missing"

#: La sonda negativa observó que la credencial de solo lectura PUEDE escribir (o divulgar más de
#: lo que el §7.2 del plan 12 permite). ``public_context.violations`` lista los motivos con
#: códigos cortos (``privilege:insert``, ``role_attribute:rolsuper``, …), nunca el texto del
#: grant. La verificación queda borrada: el servidor sale del MCP hasta corregir los grants.
CODE_READONLY_PROBE_FAILED = "server.readonly_probe_failed"

#: ``POST .../readonly-credential/provision`` encontró en el motor una cuenta con el nombre de
#: ``MCP_READONLY_ACCOUNT_USERNAME`` que NO es del gateway (este servidor no tiene guardada una
#: credencial de solo lectura con ese usuario). Rotarle la contraseña y revocarle los grants le
#: rompería la app a un tercero, así que se rechaza ANTES de mutar nada. 409.
CODE_READONLY_ACCOUNT_ALREADY_EXISTS = "readonly_account.already_exists"

#: La cuenta propia ya existe con roles (u otros grants) que ``REVOKE ALL`` no quita y que la sonda
#: reportaría como ``unrecognized_grant`` en cada reintento. 409, antes de mutar.
CODE_READONLY_ACCOUNT_HAS_ROLES = "readonly_account.has_roles"

#: Ya hay un aprovisionamiento de este servidor en curso (lock por servidor, solo dentro del
#: proceso). 409, no se cambió nada: reintentar cuando termine.
CODE_READONLY_PROVISION_IN_PROGRESS = "readonly_provision.in_progress"

ERROR_CODES = frozenset(
    {
        CODE_READONLY_ACCOUNT_ALREADY_EXISTS,
        CODE_READONLY_ACCOUNT_HAS_ROLES,
        CODE_READONLY_PROVISION_IN_PROGRESS,
        CODE_CREDENTIAL_REQUIRED_FOR_REBIND,
        CODE_READONLY_CREDENTIAL_MISSING,
        CODE_READONLY_PROBE_FAILED,
    }
)
