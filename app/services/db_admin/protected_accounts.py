"""
Cuentas del MOTOR que el gateway nunca modifica (guard anti toma de control).

POR QUÉ EXISTE. El único guard previo (``_guard_not_root``) comparaba contra
``Server.root_username``: la credencial pseudo-root DEL GATEWAY. Cualquier otra cuenta de
administración quedaba al alcance de ``engine_users.write`` (operator). Un
``PATCH /servers/{id}/users/password {username: "root"}`` ejecutaba ``ALTER USER root …``
con una contraseña elegida por el operador, y un ``add-host {username: "root",
new_host: "%", copy_grants: true}`` creaba ``root@%`` y le replicaba ``GRANT ALL ON *.*
… WITH GRANT OPTION``. Con eso el operador pasaba a ser superusuario directo del motor,
sin pasar por ``databases.drop``, ``sql_console.execute``, ``exports.download`` ni la
auditoría.

Protegida = cualquiera de:

1. La credencial pseudo-root del gateway (``Server.root_username``), comparada sin
   distinguir mayúsculas. Tocarla deja al gateway sin control del servidor.
2. Un nombre RESERVADO del motor o de la nube administrada (tablas de abajo), en
   CUALQUIER host y sin distinguir mayúsculas. En PostgreSQL, además, todo nombre con
   prefijo ``pg_`` (reservado por el propio motor para roles predefinidos).
3. Solo PostgreSQL: un rol con ``rolsuper``/``rolcreaterole``/``rolreplication``/
   ``rolbypassrls`` o miembro de un rol predefinido de administración. Se consulta en el
   motor (``adapter.is_privileged_role``) porque una lista de nombres no alcanza: el DBA
   del cliente se llama como quiera, y antes de PG16 un CREATEROLE puede alterar a
   cualquier rol no superusuario. Si la consulta falla, se RECHAZA (fail-closed).

MySQL/MariaDB se protegen solo por nombre (1 y 2). Detectar un DBA con otro nombre exige
interpretar ``SHOW GRANTS`` (privilegios globales, roles, dinámicos); queda fuera de este
guard. Lo que sí se cierra es la CLONACIÓN de privilegios globales vía ``add-host`` +
``copy_grants``: ``MySQLAdapter._rewrite_grant_line`` descarta toda línea ``ON *.*``.
"""

from app.exceptions import AppHttpException
from app.services.engine_user_catalog import (
    CODE_PROTECTED_ACCOUNT,
    CODE_PROTECTION_UNVERIFIABLE,
)

# Cuentas internas de MySQL/MariaDB. Es la MISMA tupla que ``mysql_adapter`` usa para
# filtrar los listados (``_SYSTEM_USERS``): una sola fuente, para que "no se lista" y "no se
# toca" no puedan divergir.
MYSQL_SYSTEM_USERS: tuple[str, ...] = (
    "mysql.sys",
    "mysql.session",
    "mysql.infoschema",
    "root",
    "mariadb.sys",
    "debian-sys-maint",
)

# Administradores de nubes administradas (RDS/Aurora, Cloud SQL, Azure). No se filtran de
# los listados (el operador tiene que poder verlos), pero no se modifican.
_MYSQL_MANAGED_ADMINS: tuple[str, ...] = (
    "rdsadmin",
    "rdsrepladmin",
    "rdsproxyadmin",
    "cloudsqladmin",
    "cloudsqlimport",
    "cloudsqlreplica",
    "cloudsqlsuperuser",
    "azure_superuser",
)

_PG_RESERVED: tuple[str, ...] = (
    "postgres",
    "rdsadmin",
    "rdsrepladmin",
    "rds_superuser",
    "cloudsqladmin",
    "cloudsqlsuperuser",
    "cloudsqlreplica",
    "azuresu",
    "azure_superuser",
    "azure_pg_admin",
)

_RESERVED_BY_FAMILY: dict[str, frozenset[str]] = {
    "mysql": frozenset(u.lower() for u in MYSQL_SYSTEM_USERS + _MYSQL_MANAGED_ADMINS),
    "postgresql": frozenset(u.lower() for u in _PG_RESERVED),
}

#: Roles predefinidos cuya membresía equivale a administración del servidor (PostgreSQL).
#: Los usa ``PostgresAdapter.is_privileged_role``. Son CONSTANTES internas: se interpolan en
#: la consulta, nunca input del usuario.
PG_ADMIN_ROLES: tuple[str, ...] = (
    "pg_read_all_data",
    "pg_write_all_data",
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
    "rds_superuser",
    "cloudsqlsuperuser",
    "azure_pg_admin",
)

REASON_GATEWAY_CREDENTIAL = "gateway_credential"
REASON_RESERVED_ACCOUNT = "reserved_account"
REASON_PRIVILEGED_ROLE = "privileged_role"


def _family(dialect: str) -> str:
    return "postgresql" if dialect == "postgresql" else "mysql"


def protected_reason_by_name(
    *, dialect: str, username: str, root_username: str | None
) -> str | None:
    """
    Motivo de protección que se decide SIN tocar el motor (1 y 2 del módulo), o ``None``.

    Puro: es lo que se evalúa primero en cada camino, antes de construir el adapter.
    """
    name = (username or "").strip().lower()
    if root_username and name == root_username.strip().lower():
        return REASON_GATEWAY_CREDENTIAL
    family = _family(dialect)
    if name in _RESERVED_BY_FAMILY[family]:
        return REASON_RESERVED_ACCOUNT
    if family == "postgresql" and name.startswith("pg_"):
        return REASON_RESERVED_ACCOUNT
    return None


_MESSAGES = {
    REASON_GATEWAY_CREDENTIAL: (
        "No se puede operar sobre la propia credencial pseudo-root del gateway (riesgo de "
        "auto-bloqueo). Para gestionar esa cuenta, hazlo fuera del gateway."
    ),
    REASON_RESERVED_ACCOUNT: (
        "La cuenta es una cuenta reservada del motor o de la nube administrada y el "
        "gateway no la modifica. Gestiónala fuera del gateway."
    ),
    REASON_PRIVILEGED_ROLE: (
        "La cuenta tiene privilegios de administración del servidor (superusuario, "
        "creación de roles, replicación o equivalente) y el gateway no la modifica. "
        "Gestiónala fuera del gateway."
    ),
}


def _raise_protected(username: str, reason: str) -> None:
    raise AppHttpException(
        message=_MESSAGES[reason],
        status_code=409,
        context={"username": username},
        public_context={"code": CODE_PROTECTED_ACCOUNT, "reason": reason},
    )


def assert_not_protected_by_name(
    *, dialect: str, username: str, root_username: str | None
) -> None:
    """409 ``engine_user.protected_account`` si la cuenta está protegida por NOMBRE."""
    reason = protected_reason_by_name(
        dialect=dialect, username=username, root_username=root_username
    )
    if reason is not None:
        _raise_protected(username, reason)


def assert_not_privileged_role(adapter, *, dialect: str, username: str) -> None:
    """
    Solo PostgreSQL: 409 si el rol tiene atributos de administración (3 del módulo).

    Se llama JUSTO ANTES de la primera mutación en el motor, después de las validaciones
    locales, para no pagar una consulta remota en un request que igual se iba a rechazar.

    **Fail-closed**: si el adapter no sabe responder (no implementa el chequeo, el motor
    no contesta o ``pg_roles`` no se puede leer), se rechaza con
    ``engine_user.protection_unverifiable``. Nunca se vuelca ``str(exc)``: puede llevar
    host o usuario.
    """
    if dialect != "postgresql":
        return
    check = getattr(adapter, "is_privileged_role", None)
    try:
        privileged = bool(check(username)) if callable(check) else None
    except Exception:  # noqa: BLE001 — cualquier falla es "no verificable", nunca "libre"
        privileged = None
    if privileged is None:
        raise AppHttpException(
            message=(
                "No se pudo verificar en el motor si la cuenta tiene privilegios de "
                "administración; por seguridad la operación no se ejecuta. Reintenta "
                "cuando el servidor responda."
            ),
            status_code=409,
            context={"username": username},
            public_context={"code": CODE_PROTECTION_UNVERIFIABLE},
        )
    if privileged:
        _raise_protected(username, REASON_PRIVILEGED_ROLE)


def assert_not_protected(
    adapter, *, dialect: str, username: str, root_username: str | None
) -> None:
    """Nombre + atributos en un solo llamado (para caminos sin validaciones intermedias)."""
    assert_not_protected_by_name(dialect=dialect, username=username, root_username=root_username)
    assert_not_privileged_role(adapter, dialect=dialect, username=username)
