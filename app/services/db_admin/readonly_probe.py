"""
Evaluación PURA de la sonda negativa de la credencial de solo lectura (plan 12 §5.2, §7.2).

Los adapters leen los hechos del motor (``SHOW GRANTS`` en la familia MySQL, atributos y
privilegios del rol en PostgreSQL) y este módulo decide. Está separado del adapter para que la
regla se pruebe sin un motor: la parte que más importa acertar es la de clasificar, no la de leer.

LA REGLA ES UNA ALLOWLIST, Y TODO LO QUE NO SE ENTIENDE ES UNA VIOLACIÓN
-----------------------------------------------------------------------
Un ``GRANT`` que no matchea el patrón conocido (un rol otorgado, un ``PROXY``, una sintaxis de
una versión nueva) no se ignora: se reporta. Una sonda que deja pasar lo que no entiende es una
sonda que aprueba cualquier credencial nueva por default, que es justo lo contrario de su función.

POR QUÉ ``TRIGGER`` Y ``EVENT`` ESTÁN PERMITIDOS AUNQUE PERMITEN CREAR OBJETOS
-----------------------------------------------------------------------------
En MySQL/MariaDB son los únicos privilegios que dejan VER triggers y events, y su ausencia es
silenciosa: el catálogo devuelve cero filas en vez de un error (plan 12 §3.3). El §7.2 los incluye
en los grants mínimos a sabiendas. La defensa contra que se usen para escribir es la otra mitad:
la sesión del MCP corre en ``TRANSACTION READ ONLY`` y ninguna tool EJECUTA SQL del agente
(``draft_query`` lo acepta como texto y nunca abre una conexión).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Privilegios que la credencial puede tener en la familia MySQL (§7.2). ``USAGE`` es "ninguno".
MYSQL_ALLOWED_PRIVILEGES = frozenset(
    {"USAGE", "SELECT", "SHOW VIEW", "TRIGGER", "EVENT", "SHOW_ROUTINE"}
)

#: Privilegios que se aceptan sobre ``*.*``. ``SELECT ON *.*`` NO: incluye ``mysql.servers``, que
#: guarda usuario y contraseña en claro (§7.2, "nunca ``SELECT ON mysql.*``").
_MYSQL_GLOBAL_OK = frozenset({"USAGE", "SHOW_ROUTINE"})

#: Lo que el aprovisionamiento AUTOMÁTICO otorga (``POST .../readonly-credential/provision``). Son
#: constantes del servidor: ningún request las toca. Viven acá, junto a la allowlist que las
#: juzga, para que no puedan divergir: lo que se otorga TIENE que pasar la sonda. Un grant nuevo
#: acá que la allowlist no conoce haría fallar toda credencial aprovisionada, y el import de
#: abajo lo detecta en el arranque en vez de en producción.
MYSQL_READONLY_DB_GRANTS: tuple[str, ...] = ("SELECT", "SHOW VIEW", "TRIGGER", "EVENT")
#: ``SHOW_ROUTINE`` es dinámico (8.0.20+), no scopeable a una base, y MariaDB no lo tiene (§7.2).
MYSQL_READONLY_GLOBAL_GRANTS: tuple[str, ...] = ("SHOW_ROUTINE",)

assert set(MYSQL_READONLY_DB_GRANTS) <= MYSQL_ALLOWED_PRIVILEGES
assert set(MYSQL_READONLY_GLOBAL_GRANTS) <= _MYSQL_GLOBAL_OK

#: Primera versión de MySQL que tiene el privilegio dinámico ``SHOW_ROUTINE``. Antes, el ``GRANT``
#: falla con un error de sintaxis, y si falla DESPUÉS de rotar y revocar la cuenta queda a medias.
MYSQL_SHOW_ROUTINE_MIN_VERSION: tuple[int, int, int] = (8, 0, 20)

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class ReadonlyPreflight:
    """
    Hechos que el adapter lee del motor ANTES de la primera sentencia que muta.

    ``exists``: la cuenta ya existe en el motor. ``global_grants``: privilegios ``*.*`` que se
    pueden otorgar en ESTE servidor (vacío en MariaDB, en MySQL < 8.0.20 y si la versión no se
    pudo determinar). ``note``: texto corto, sin secretos, para el detalle de auditoría.
    """

    exists: bool
    global_grants: tuple[str, ...] = ()
    note: str | None = None


def mysql_global_grants_for_version(
    version: str | None, *, dialect: str
) -> tuple[tuple[str, ...], str | None]:
    """
    ``(grants globales a otorgar, nota)`` según el motor y su versión. PURA.

    ``SHOW_ROUTINE`` solo en MySQL >= 8.0.20: nunca en MariaDB (ni en un servidor registrado como
    ``mysql`` cuya versión diga MariaDB) y, ante una versión ilegible, tampoco: un "no sé" que
    otorgara igual rompería el aprovisionamiento a mitad de camino.
    """
    if dialect != "mysql" or "mariadb" in (version or "").lower():
        return (), "SHOW_ROUTINE no aplica a MariaDB"
    m = _VERSION_RE.match(version or "")
    if m is None:
        return (), "versión del servidor no determinada: SHOW_ROUTINE no otorgado"
    if tuple(int(g) for g in m.groups()) < MYSQL_SHOW_ROUTINE_MIN_VERSION:
        return (), "MySQL < 8.0.20: SHOW_ROUTINE no existe y no se otorgó"
    return MYSQL_READONLY_GLOBAL_GRANTS, None


def mysql_has_unrecognized_grants(lines: list[str]) -> bool:
    """
    ¿``SHOW GRANTS FOR <cuenta>`` trae algo que ``REVOKE ALL PRIVILEGES, GRANT OPTION`` no quita?

    Un rol otorgado (``GRANT `r`@`%` TO ...``) o un ``PROXY`` (que ``REVOKE ALL`` tampoco quita) no están en la lista: sobreviven al
    ``REVOKE ALL`` y la sonda los reporta como ``unrecognized_grant`` en cada reintento. Se detecta
    ANTES de mutar, para rechazar en vez de dejar la cuenta rotada y sin poder verificarse.
    """
    for line in lines:
        texto = (line or "").strip()
        if not texto:
            continue
        if _GRANT_RE.match(texto) is None or re.match(r"^GRANT\s+PROXY\b", texto, re.IGNORECASE):
            return True
    return False


_GRANT_RE = re.compile(r"^GRANT\s+(?P<privs>.+?)\s+ON\s+(?P<obj>\S+)\s+TO\s+", re.IGNORECASE)


def _split_privileges(raw: str) -> list[str]:
    """``SELECT (a, b), SHOW VIEW`` → ``["SELECT", "SHOW VIEW"]``: las listas de columnas se van."""
    sin_columnas = re.sub(r"\([^)]*\)", "", raw)
    return [p.strip().upper() for p in sin_columnas.split(",") if p.strip()]


def _normalize_object(obj: str) -> str:
    return obj.replace("`", "").replace('"', "").lower()


def mysql_grant_violations(lines: list[str]) -> list[str]:
    """
    Motivos por los que un ``SHOW GRANTS FOR CURRENT_USER()`` permite escribir o divulgar.

    Devuelve códigos cortos y estables (sin el texto del grant: puede llevar el host de la
    cuenta), en el orden en que aparecen y sin repetir.
    """
    out: list[str] = []

    def add(code: str) -> None:
        if code not in out:
            out.append(code)

    for line in lines:
        texto = (line or "").strip()
        if not texto:
            continue
        if "WITH GRANT OPTION" in texto.upper():
            add("grant_option")
        m = _GRANT_RE.match(texto)
        if m is None:
            # Rol otorgado (`GRANT r TO u`), `PROXY`, o una forma que esta sonda no conoce.
            add("unrecognized_grant")
            continue
        obj = _normalize_object(m.group("obj"))
        for priv in _split_privileges(m.group("privs")):
            if priv in ("ALL", "ALL PRIVILEGES"):
                add("all_privileges")
                continue
            if priv not in MYSQL_ALLOWED_PRIVILEGES:
                add(f"privilege:{priv.lower().replace(' ', '_')}")
                continue
            if obj == "*.*" and priv not in _MYSQL_GLOBAL_OK:
                add(f"global_privilege:{priv.lower().replace(' ', '_')}")
            elif obj.startswith("mysql.") and priv == "SELECT" and obj != "mysql.proc":
                add("select_on_mysql_schema")
    return out


def postgres_role_violations(facts: dict) -> list[str]:
    """
    Motivos por los que el rol de PostgreSQL puede escribir, a partir de los hechos que lee el
    adapter. Una clave AUSENTE cuenta como violación: si el adapter no pudo leer un hecho, la
    sonda no lo asume favorable.
    """
    out: list[str] = []
    for attr in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"):
        if facts.get(attr, True):
            out.append(f"role_attribute:{attr}")
    if facts.get("default_transaction_read_only") != "on":
        # Es la única protección de PG atada a la CUENTA (§7.2): sin ella, la promesa depende de
        # que todo cliente se acuerde de abrir la sesión en solo lectura.
        out.append("default_transaction_read_only_off")
    if facts.get("can_create_in_database", True):
        out.append("create_on_database")
    if facts.get("can_create_in_public", True):
        out.append("create_on_schema_public")
    for role in facts.get("write_roles", []):
        out.append(f"member_of:{role}")
    if facts.get("table_write_privileges", 1):
        out.append("table_write_privileges")
    if facts.get("temp_write_succeeded", True):
        out.append("write_attempt_succeeded")
    return out


__all__ = [
    "MYSQL_SHOW_ROUTINE_MIN_VERSION",
    "ReadonlyPreflight",
    "mysql_global_grants_for_version",
    "mysql_has_unrecognized_grants",
    "MYSQL_ALLOWED_PRIVILEGES",
    "MYSQL_READONLY_DB_GRANTS",
    "MYSQL_READONLY_GLOBAL_GRANTS",
    "mysql_grant_violations",
    "postgres_role_violations",
]
