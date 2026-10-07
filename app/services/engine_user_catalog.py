"""
Vocabulario cerrado de códigos de error del manejo de USUARIOS DEL MOTOR (no del gateway).

Viajan en ``public_context["code"]``, **nunca** en ``context``: ``context`` solo se expone en
``development``, así que en producción el operador recibiría el 409 sin saber por qué la
cuenta está protegida ni qué hacer. Los emiten ``ServerUserController`` y
``GrantController`` a través de ``db_admin.protected_accounts``.
"""

#: La cuenta está PROTEGIDA: es la credencial pseudo-root del propio gateway, una cuenta
#: reservada del motor o de la nube administrada (``root``, ``mysql.sys``, ``postgres``,
#: ``rdsadmin``…), o —en PostgreSQL— un rol con atributos de administración (SUPERUSER,
#: CREATEROLE, REPLICATION, BYPASSRLS o membresía en un rol predefinido de administración).
#: Ninguna operación que la modifique se ejecuta desde el gateway. ``public_context.reason``
#: dice cuál de los tres casos es.
CODE_PROTECTED_ACCOUNT = "engine_user.protected_account"

#: No se pudo VERIFICAR en el motor si la cuenta es un rol de administración (PostgreSQL:
#: ``pg_roles`` ilegible o motor caído). Fail-closed: se rechaza igual que una protegida, con
#: un código distinto para que el cliente ofrezca reintentar en vez de "no se puede".
CODE_PROTECTION_UNVERIFIABLE = "engine_user.protection_unverifiable"

#: El actor tiene ``engine_users.write`` pero el payload pide delegar privilegios (WITH GRANT OPTION,
#: un privilegio sensible, o ``provision`` al reasignar el dueño de una base) y le falta
#: ``engine_users.grant_admin`` (solo ``owner``). 403. A diferencia del ``access.forbidden`` opaco,
#: este SÍ nombra la capacidad: quien llega acá ya tiene la capacidad base y eligió el payload que
#: escala, así que el mensaje no le revela nada nuevo y la SPA puede explicar qué falta.
#: ``public_context`` lleva ``required_capability`` y ``reason`` (uno de ``GRANT_ADMIN_REASONS``).
CODE_GRANT_ADMIN_REQUIRED = "engine_user.grant_admin_required"

#: Por qué el payload exige ``engine_users.grant_admin``. Vocabulario cerrado (va en
#: ``public_context["reason"]``).
GRANT_ADMIN_REASON_WITH_GRANT_OPTION = "with_grant_option"
GRANT_ADMIN_REASON_SENSITIVE_PRIVILEGE = "sensitive_privilege"
GRANT_ADMIN_REASON_PROVISION_REASSIGN_OWNER = "provision_reassign_owner"
GRANT_ADMIN_REASONS = frozenset(
    {
        GRANT_ADMIN_REASON_WITH_GRANT_OPTION,
        GRANT_ADMIN_REASON_SENSITIVE_PRIVILEGE,
        GRANT_ADMIN_REASON_PROVISION_REASSIGN_OWNER,
    }
)

#: Nombre de la capacidad que falta, para el mensaje y ``public_context``. Es un literal y no
#: ``Capability.ENGINE_USERS_GRANT_ADMIN.value`` para que este módulo siga siendo vocabulario puro,
#: sin importar el catálogo de capacidades; un test fija que coinciden.
GRANT_ADMIN_CAPABILITY_ID = "engine_users.grant_admin"

_GRANT_ADMIN_MESSAGE_BY_REASON = {
    GRANT_ADMIN_REASON_WITH_GRANT_OPTION: (
        "Otorgar con WITH GRANT OPTION requiere la capacidad 'engine_users.grant_admin', "
        "además de 'engine_users.write'."
    ),
    GRANT_ADMIN_REASON_SENSITIVE_PRIVILEGE: (
        "Otorgar un privilegio sensible requiere la capacidad 'engine_users.grant_admin', "
        "además de 'engine_users.write'."
    ),
    GRANT_ADMIN_REASON_PROVISION_REASSIGN_OWNER: (
        "Reasignar el dueño aplicándolo en el motor (provision=true) requiere la capacidad "
        "'engine_users.grant_admin', además de 'databases.drop'."
    ),
}


def grant_admin_required_message(reason: str) -> str:
    """Mensaje en español del 403 ``CODE_GRANT_ADMIN_REQUIRED`` para ``reason`` (``GRANT_ADMIN_REASONS``)."""
    return _GRANT_ADMIN_MESSAGE_BY_REASON[reason]


ERROR_CODES = frozenset(
    {CODE_PROTECTED_ACCOUNT, CODE_PROTECTION_UNVERIFIABLE, CODE_GRANT_ADMIN_REQUIRED}
)
