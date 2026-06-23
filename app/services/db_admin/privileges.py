"""
Catálogo de privilegios y validación por motor y nivel (SEGURIDAD CRÍTICA).

Reemplaza la validación laxa de `identifiers.validate_privileges` (un regex que
aceptaba cualquier palabra en mayúsculas) por enumeraciones CERRADAS por
``(motor, nivel)``. Reglas de oro:

  1. Un privilegio NUNCA se interpola desde el input: se valida contra un set fijo
     y se devuelve el TOKEN CANÓNICO (constante interna), que es lo único que el
     adapter interpola en el DCL.
  2. La tabla de compatibilidad es por NIVEL: ``EXECUTE`` no aplica a tabla,
     ``TRUNCATE`` no aplica a secuencia, etc.
  3. Tres clases de privilegio:
       - ALLOW: object-level CRUD/DDL, otorgable directo (sujeto al pre-chequeo
         de capability del grantor, que vive en el adapter).
       - GATE:  otorgable pero requiere doble confirmación (``ALL PRIVILEGES``,
         ``GRANT OPTION``, PG ``MAINTAIN``). También ``with_grant_option``.
       - DENY:  privilegios administrativos jamás otorgables por esta feature.

Ver docs/plans/07-gestion-granular-de-permisos.md (§1, §6).
"""

from app.exceptions import AppHttpException
from app.services.db_admin.dtos import GrantLevel

# Familias de dialecto. MariaDB = MySQL + extras propios.
_MYSQL_FAMILY = ("mysql", "mariadb")


def _family(dialect: str) -> str:
    return "mysql" if dialect in _MYSQL_FAMILY else dialect


def _norm(token: str) -> str:
    """Normaliza un token: mayúsculas y espacios internos colapsados a uno."""
    return " ".join(str(token).upper().split())


# Alias de entrada -> token canónico.
_ALIASES = {
    "ALL": "ALL PRIVILEGES",
    "TEMP": "TEMPORARY",
    # MariaDB acepta REPLICA como sinónimo de SLAVE en algunos tokens; los de
    # replicación están en DENY de todos modos.
}

# ---------------------------------------------------------------------------
# Catálogo ALLOW por (familia, nivel). Tokens canónicos.
# ---------------------------------------------------------------------------

# MySQL/MariaDB: privilegios de tabla (también válidos a nivel database).
_MYSQL_TABLE = frozenset(
    {
        "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
        "INDEX", "REFERENCES", "CREATE VIEW", "SHOW VIEW", "TRIGGER",
    }
)
# Database añade los privilegios que solo tienen sentido a ese nivel.
_MYSQL_DB = _MYSQL_TABLE | {
    "CREATE ROUTINE", "ALTER ROUTINE", "EXECUTE",
    "CREATE TEMPORARY TABLES", "LOCK TABLES", "EVENT",
}
_MYSQL_COLUMN = frozenset({"SELECT", "INSERT", "UPDATE", "REFERENCES"})
_MYSQL_ROUTINE = frozenset({"EXECUTE", "ALTER ROUTINE"})

# Extras exclusivos de MariaDB 11.x respecto de MySQL.
_MARIADB_TABLE_EXTRA = frozenset({"DELETE HISTORY"})

_ALLOW: dict[str, dict[GrantLevel, frozenset[str]]] = {
    "mysql": {
        GrantLevel.DATABASE: _MYSQL_DB,
        GrantLevel.TABLE: _MYSQL_TABLE,
        GrantLevel.COLUMN: _MYSQL_COLUMN,
        GrantLevel.ROUTINE: _MYSQL_ROUTINE,
    },
    "mariadb": {
        GrantLevel.DATABASE: _MYSQL_DB | _MARIADB_TABLE_EXTRA,
        GrantLevel.TABLE: _MYSQL_TABLE | _MARIADB_TABLE_EXTRA,
        GrantLevel.COLUMN: _MYSQL_COLUMN,
        GrantLevel.ROUTINE: _MYSQL_ROUTINE,
    },
    "postgresql": {
        GrantLevel.DATABASE: frozenset({"CONNECT", "CREATE", "TEMPORARY"}),
        GrantLevel.SCHEMA: frozenset({"USAGE", "CREATE"}),
        GrantLevel.TABLE: frozenset(
            {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
        ),
        GrantLevel.COLUMN: frozenset({"SELECT", "INSERT", "UPDATE", "REFERENCES"}),
        GrantLevel.SEQUENCE: frozenset({"USAGE", "SELECT", "UPDATE"}),
        GrantLevel.ROUTINE: frozenset({"EXECUTE"}),
    },
}

# ---------------------------------------------------------------------------
# Catálogo GATE por (familia, nivel): otorgable con doble confirmación.
# ---------------------------------------------------------------------------
_ALL = "ALL PRIVILEGES"

_GATE: dict[str, dict[GrantLevel, frozenset[str]]] = {
    "mysql": {
        GrantLevel.DATABASE: frozenset({_ALL, "GRANT OPTION"}),
        GrantLevel.TABLE: frozenset({_ALL, "GRANT OPTION"}),
        GrantLevel.COLUMN: frozenset({_ALL}),
        GrantLevel.ROUTINE: frozenset({_ALL, "GRANT OPTION"}),
    },
    "mariadb": {
        GrantLevel.DATABASE: frozenset({_ALL, "GRANT OPTION"}),
        GrantLevel.TABLE: frozenset({_ALL, "GRANT OPTION"}),
        GrantLevel.COLUMN: frozenset({_ALL}),
        GrantLevel.ROUTINE: frozenset({_ALL, "GRANT OPTION"}),
    },
    "postgresql": {
        GrantLevel.DATABASE: frozenset({_ALL}),
        GrantLevel.SCHEMA: frozenset({_ALL}),
        GrantLevel.TABLE: frozenset({_ALL, "MAINTAIN"}),
        GrantLevel.COLUMN: frozenset({_ALL}),
        GrantLevel.SEQUENCE: frozenset({_ALL}),
        GrantLevel.ROUTINE: frozenset({_ALL}),
    },
}

# ---------------------------------------------------------------------------
# Catálogo DENY: privilegios administrativos jamás otorgables por esta feature.
# Se rechazan en CUALQUIER nivel (defensa en profundidad; los niveles object-level
# tampoco los listan en ALLOW, pero el DENY explícito da un 422 claro y específico).
# ---------------------------------------------------------------------------
_DENY: dict[str, frozenset[str]] = {
    "mysql": frozenset(
        {
            "SUPER", "FILE", "PROCESS", "RELOAD", "SHUTDOWN", "CREATE USER",
            "SHOW DATABASES", "REPLICATION CLIENT", "REPLICATION SLAVE",
            "REPLICATION REPLICA", "BINLOG ADMIN", "BINLOG MONITOR",
            "BINLOG REPLAY", "CONNECTION ADMIN", "FEDERATED ADMIN",
            "READ_ONLY ADMIN", "REPLICA MONITOR", "REPLICATION MASTER ADMIN",
            "REPLICATION SLAVE ADMIN", "REPLICATION REPLICA ADMIN", "SET USER",
            "SLAVE MONITOR", "SYSTEM_USER", "ROLE_ADMIN", "PROXY",
        }
    ),
    "postgresql": frozenset(
        {
            # Atributos de rol (no son privilegios de objeto): bloqueo defensivo.
            "SUPERUSER", "CREATEROLE", "CREATEDB", "REPLICATION", "BYPASSRLS",
        }
    ),
}
# MariaDB comparte el DENY de la familia MySQL.
_DENY["mariadb"] = _DENY["mysql"]


def is_level_supported(dialect: str, level: GrantLevel) -> bool:
    """¿El motor admite otorgar a este nivel en la Fase 1?"""
    return level in _ALLOW.get(dialect, {})


def supported_levels(dialect: str) -> list[GrantLevel]:
    return list(_ALLOW.get(dialect, {}).keys())


def validate_privileges(
    privileges: list[str], dialect: str, level: GrantLevel
) -> tuple[list[str], bool]:
    """
    Valida una lista de privilegios para ``(dialect, level)``.

    Devuelve ``(tokens_canonicos, requires_confirmation)``:
      - ``tokens_canonicos``: lista deduplicada de constantes internas a interpolar.
      - ``requires_confirmation``: True si algún token cae en el set GATE.

    Lanza ``AppHttpException(422)`` si: el nivel no es soportado por el motor, la
    lista está vacía, un token es administrativo (DENY) o no es válido para el nivel.
    NUNCA refleja el token crudo del usuario en el mensaje (evita reflejar payloads).
    """
    if dialect not in _ALLOW:
        raise AppHttpException(
            message=f"Motor de base de datos no soportado: {dialect}",
            status_code=422,
            context={"dialect": dialect},
        )
    if not is_level_supported(dialect, level):
        raise AppHttpException(
            message="El nivel de permiso no está soportado para este motor.",
            status_code=422,
            context={
                "dialect": dialect,
                "level": level.value,
                "supported": [lvl.value for lvl in supported_levels(dialect)],
            },
        )
    if not privileges:
        raise AppHttpException(
            message="Se requiere al menos un privilegio.",
            status_code=422,
            context={"level": level.value},
        )

    allow = _ALLOW[dialect][level]
    gate = _GATE[dialect].get(level, frozenset())
    deny = _DENY[dialect]

    canonical: list[str] = []
    seen: set[str] = set()
    requires_confirmation = False

    for raw in privileges:
        token = _norm(raw)
        if not token:
            raise AppHttpException(
                message="Privilegio vacío o inválido.",
                status_code=422,
                context={"level": level.value},
            )
        token = _ALIASES.get(token, token)

        if token in deny:
            raise AppHttpException(
                message="Privilegio administrativo no permitido por la plataforma.",
                status_code=422,
                context={"level": level.value, "class": "deny"},
            )
        if token in gate:
            requires_confirmation = True
        elif token not in allow:
            raise AppHttpException(
                message="Privilegio inválido para el nivel o el motor indicado.",
                status_code=422,
                context={
                    "level": level.value,
                    "dialect": dialect,
                    "allowed": sorted(allow | gate),
                },
            )

        if token not in seen:
            seen.add(token)
            canonical.append(token)

    return canonical, requires_confirmation


def controlled_tokens(dialect: str) -> set[str]:
    """
    Todos los tokens (ALLOW ∪ GATE), en cualquier nivel, que la plataforma controla
    para ``dialect``. Es la fuente para sembrar la tabla `privileges` como ACTIVOS,
    garantizando que lo "activo" en BD coincide con lo validable en código.
    """
    out: set[str] = set()
    for toks in _ALLOW.get(dialect, {}).values():
        out |= toks
    for toks in _GATE.get(dialect, {}).values():
        out |= toks
    return out


def token_is_sensitive(dialect: str, token: str) -> bool:
    """True si el token es del set GATE (requiere confirmación) en algún nivel."""
    norm = _ALIASES.get(_norm(token), _norm(token))
    return any(norm in toks for toks in _GATE.get(dialect, {}).values())


def classify(token: str, dialect: str, level: GrantLevel) -> str:
    """Devuelve 'allow' | 'gate' | 'deny' | 'invalid' para un token (introspección/tests)."""
    if dialect not in _ALLOW or not is_level_supported(dialect, level):
        return "invalid"
    norm = _ALIASES.get(_norm(token), _norm(token))
    if norm in _DENY[dialect]:
        return "deny"
    if norm in _GATE[dialect].get(level, frozenset()):
        return "gate"
    if norm in _ALLOW[dialect][level]:
        return "allow"
    return "invalid"
