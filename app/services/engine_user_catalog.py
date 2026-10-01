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

ERROR_CODES = frozenset({CODE_PROTECTED_ACCOUNT, CODE_PROTECTION_UNVERIFIABLE})
