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
la sesión del MCP corre en ``TRANSACTION READ ONLY`` y esta credencial de ESTRUCTURA nunca ve SQL
del agente (``draft_query`` lo acepta como texto sin conexión; ``run_select`` ejecuta solo ``SELECT``
validados con la credencial de DATOS de la base, no con esta).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

#: Privilegio de MariaDB >= 11.3 (MDEV-29167) que deja leer el código de las rutinas de UNA base
#: sin ``SELECT ON mysql.proc``. ÚNICO lugar donde vive el literal: el aprovisionamiento, la
#: allowlist de la sonda y el helper de versión lo toman de acá.
#: SIN CONFIRMAR EN STAGING: el nombre exacto y la sintaxis del ``GRANT`` salen de la
#: documentación de MariaDB, no de un servidor 11.3+ probado. Confirmar con ``GRANT SHOW CREATE
#: ROUTINE ON `db`.* TO ...`` y con la salida de ``SHOW GRANTS`` ANTES de habilitar en producción;
#: si el nombre difiere, se corrige solo esta constante.
MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE = "SHOW CREATE ROUTINE"

#: Privilegios que la credencial puede tener en la familia MySQL (§7.2). ``USAGE`` es "ninguno".
MYSQL_ALLOWED_PRIVILEGES = frozenset(
    {
        "USAGE",
        "SELECT",
        "SHOW VIEW",
        "TRIGGER",
        "EVENT",
        "SHOW_ROUTINE",
        MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE,
    }
)

#: Privilegios de la allowlist que solo existen en MariaDB: en MySQL (u otro motor) verlos en una
#: cuenta es una anomalía y la sonda los reporta, aunque estén en ``MYSQL_ALLOWED_PRIVILEGES``.
_MARIADB_ONLY_PRIVILEGES = frozenset({MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE})

#: Única tabla del esquema ``mysql`` sobre la que el aprovisionamiento puede otorgar ``SELECT``, y
#: solo con la bandera ``servers.readonly_proc_grant`` encendida en un motor que la necesita
#: (MariaDB < 11.3, MySQL < 8.0). ÚNICO lugar donde vive el literal: el ``GRANT`` y la sonda lo
#: toman de acá. Es SERVER-WIDE: expone el código de las rutinas de todas las bases del servidor.
MYSQL_PROC_TABLE = "mysql.proc"

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
#: Grants por base que se SUMAN solo en MariaDB >= 11.3 (``mariadb_routine_grants_for_version``).
#: Nunca globales y nunca sobre ``mysql.*``: el flag server-wide de ``mysql.proc`` es otro camino.
MARIADB_READONLY_DB_EXTRA_GRANTS: tuple[str, ...] = (MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE,)

assert set(MYSQL_READONLY_DB_GRANTS) <= MYSQL_ALLOWED_PRIVILEGES
assert set(MYSQL_READONLY_GLOBAL_GRANTS) <= _MYSQL_GLOBAL_OK
assert set(MARIADB_READONLY_DB_EXTRA_GRANTS) <= MYSQL_ALLOWED_PRIVILEGES
assert set(MARIADB_READONLY_DB_EXTRA_GRANTS) <= _MARIADB_ONLY_PRIVILEGES
assert not set(MARIADB_READONLY_DB_EXTRA_GRANTS) & _MYSQL_GLOBAL_OK

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
    ``db_extra_grants``: privilegios que se SUMAN a ``MYSQL_READONLY_DB_GRANTS`` en cada base
    (``SHOW CREATE ROUTINE`` en MariaDB >= 11.3; vacío en cualquier otro motor o versión). Va al
    final para no mover los campos posicionales existentes.

    ``proc_grant_supported``: el motor y su versión necesitan y admiten ``SELECT ON mysql.proc``
    (``proc_grant_supported()``; lo decide el adapter con la versión que lee). ``proc_grant``:
    el aprovisionamiento DEBE otorgarlo en esta corrida. Lo fija el controller como
    ``bandera del servidor AND proc_grant_supported``; el adapter nunca lo deduce por su cuenta,
    porque la bandera es una decisión de un administrador y no un hecho del motor.
    """

    exists: bool
    global_grants: tuple[str, ...] = ()
    note: str | None = None
    db_extra_grants: tuple[str, ...] = ()
    proc_grant_supported: bool = False
    proc_grant: bool = False


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


#: Primera versión de MariaDB con el privilegio ``SHOW CREATE ROUTINE`` a nivel de base. Antes, el
#: código de una rutina solo se lee con ``SELECT ON mysql.proc`` (todas las bases del servidor).
MARIADB_SHOW_CREATE_ROUTINE_MIN_VERSION: tuple[int, int, int] = (11, 3, 0)
#: Primera versión de MySQL sin ``mysql.proc`` (lo reemplaza el diccionario de datos): desde acá
#: el grant ``SELECT ON mysql.proc`` no existe y no tiene sentido ofrecerlo.
MYSQL_NO_PROC_TABLE_MIN_VERSION: tuple[int, int, int] = (8, 0, 0)

#: MariaDB antepone ``5.5.5-`` a su versión real en ciertos handshakes (``5.5.5-10.11.6-MariaDB``);
#: sin descartarlo, la versión leída sería 5.5.5 y toda decisión por versión saldría mal.
_MARIADB_REPLICATION_PREFIX_RE = re.compile(r"^\s*5\.5\.5-(\d+\.\d+\.\d+)")

_DEFINITION_ENGINE_MYSQL_FAMILY = "mysql"
_DEFINITION_ENGINE_MARIADB = "mariadb"


def _parse_server_version(version: str | None) -> tuple[int, int, int] | None:
    """``(mayor, menor, parche)`` de la cadena de versión, o ``None`` si no se puede leer."""
    raw_version = version or ""
    prefixed = _MARIADB_REPLICATION_PREFIX_RE.match(raw_version)
    candidate = prefixed.group(1) if prefixed else raw_version
    match = _VERSION_RE.match(candidate)
    if match is None:
        return None
    major, minor, patch = (int(group) for group in match.groups())
    return major, minor, patch


def _is_mariadb(version: str | None, dialect: str) -> bool:
    return dialect == _DEFINITION_ENGINE_MARIADB or "mariadb" in (version or "").lower()


def is_mariadb_engine(version: str | None, dialect: str) -> bool:
    """
    ¿El servidor es MariaDB? Por el dialecto registrado O por la cadena de versión. PURA.

    Aprovisionamiento y sonda usan ESTA misma decisión: lo que se otorga tiene que pasar la sonda.
    """
    return _is_mariadb(version, dialect)


def mariadb_routine_grants_for_version(
    version: str | None, dialect: str = _DEFINITION_ENGINE_MYSQL_FAMILY
) -> tuple[tuple[str, ...], str | None]:
    """
    ``(grants por base a agregar, nota)``: ``SHOW CREATE ROUTINE`` solo en MariaDB >= 11.3. PURA.

    ``dialect`` es el del servidor registrado: un ``MariaDBAdapter`` cuyo ``VERSION()`` no
    trae el sufijo ``-MariaDB`` sigue siendo MariaDB.

    Ante una versión ilegible NO se otorga: un ``GRANT`` con un privilegio que el servidor no
    conoce falla con error de sintaxis DESPUÉS de rotar y revocar la cuenta, y la deja a medias
    (mismo criterio que ``mysql_global_grants_for_version``).
    """
    if not _is_mariadb(version, dialect):
        return (), "SHOW CREATE ROUTINE a nivel de base solo existe en MariaDB"
    parsed = _parse_server_version(version)
    if parsed is None:
        return (), "versión del servidor no determinada: SHOW CREATE ROUTINE no otorgado"
    if parsed < MARIADB_SHOW_CREATE_ROUTINE_MIN_VERSION:
        return (), "MariaDB < 11.3: SHOW CREATE ROUTINE no existe y no se otorgó"
    return MARIADB_READONLY_DB_EXTRA_GRANTS, None


def proc_grant_supported(version: str | None, dialect: str) -> bool:
    """
    ¿Tiene sentido ``SELECT ON mysql.proc`` en este servidor? Solo MariaDB < 11.3 y MySQL < 8.0. PURA.

    PostgreSQL no tiene ``mysql.proc``. Con una versión ilegible la respuesta es ``False``: no
    se ofrece un grant que quizás no exista.
    """
    if dialect not in (_DEFINITION_ENGINE_MYSQL_FAMILY, _DEFINITION_ENGINE_MARIADB):
        return False
    parsed = _parse_server_version(version)
    if parsed is None:
        return False
    if _is_mariadb(version, dialect):
        return parsed < MARIADB_SHOW_CREATE_ROUTINE_MIN_VERSION
    return parsed < MYSQL_NO_PROC_TABLE_MIN_VERSION


def routine_body_reason(
    engine: str, version: str | None, proc_flag: bool
) -> Literal["flag_off", "engine_unsupported"] | None:
    """
    Por qué el cuerpo de una RUTINA no está disponible según motor y versión, o ``None`` si la
    versión no lo explica (el llamador cae en ``insufficient_privilege``). PURA.

    - MariaDB < 11.3 o MySQL 5.7 (< 8.0) con la bandera ``readonly_proc_grant`` apagada:
      ``flag_off`` (un administrador PUEDE habilitarlo). Con la bandera prendida y sin cuerpo, el
      motivo ya no es la bandera: ``None``.
    - MySQL 8.0.0 a 8.0.19: ``engine_unsupported``. No hay ``mysql.proc`` ni ``SHOW_ROUTINE``
      (llega en 8.0.20), así que NO existe grant que lo arregle.
    - MySQL 8.0.20+ y MariaDB 11.3+: ``None`` (el motivo es privilegio o DEFINER).
    - PostgreSQL y versión ilegible: ``None`` (conservador).

    ``engine`` es el dialecto del gateway (``mysql``, ``mariadb`` o ``postgresql``); un servidor
    registrado como ``mysql`` cuya versión dice MariaDB se trata como MariaDB.
    """
    if engine not in (_DEFINITION_ENGINE_MYSQL_FAMILY, _DEFINITION_ENGINE_MARIADB):
        return None
    parsed = _parse_server_version(version)
    if parsed is None:
        return None
    if _is_mariadb(version, engine):
        if parsed < MARIADB_SHOW_CREATE_ROUTINE_MIN_VERSION and not proc_flag:
            return "flag_off"
        return None
    if parsed < MYSQL_NO_PROC_TABLE_MIN_VERSION:
        return None if proc_flag else "flag_off"
    if parsed < MYSQL_SHOW_ROUTINE_MIN_VERSION:
        return "engine_unsupported"
    return None


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


def mysql_grant_violations(
    lines: list[str], *, is_mariadb: bool = False, allow_mysql_proc: bool = False
) -> list[str]:
    """
    Motivos por los que un ``SHOW GRANTS FOR CURRENT_USER()`` permite escribir o divulgar.

    ``is_mariadb``: los privilegios de ``_MARIADB_ONLY_PRIVILEGES`` solo se toleran si es True, y
    solo a nivel de base (``db.*``). Por defecto False: ante la duda de motor no se asume
    el permisivo (mismo criterio que ``lower_case_table_names`` en la sonda de datos).

    ``allow_mysql_proc``: tolera ``SELECT ON mysql.proc`` y NADA más del esquema ``mysql``. Es
    True solo cuando la bandera ``readonly_proc_grant`` del servidor está encendida Y el motor la
    necesita (el llamador resuelve ambas con ``proc_grant_supported``). Con False —el default— ese
    grant es ``select_on_mysql_schema``: ``mysql.proc`` expone las rutinas de TODAS las bases del
    servidor, y una credencial que lo tiene sin que un administrador lo haya aceptado no verifica.
    ``SELECT ON mysql.*`` o sobre cualquier otra tabla de ``mysql`` es violación con cualquier
    valor: ``mysql.user`` y ``mysql.servers`` guardan secretos.

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
            if priv in _MARIADB_ONLY_PRIVILEGES and not is_mariadb:
                add(f"privilege:{priv.lower().replace(' ', '_')}")
            elif obj == "*.*" and priv not in _MYSQL_GLOBAL_OK:
                add(f"global_privilege:{priv.lower().replace(' ', '_')}")
            elif priv in _MARIADB_ONLY_PRIVILEGES and not obj.endswith(".*"):
                # Solo a nivel de base: sobre una tabla o rutina suelta no es lo que se aprovisiona.
                add(f"non_database_privilege:{priv.lower().replace(' ', '_')}")
            elif obj.startswith("mysql.") and priv == "SELECT":
                is_tolerated_proc_grant = allow_mysql_proc and obj == MYSQL_PROC_TABLE
                if not is_tolerated_proc_grant:
                    add("select_on_mysql_schema")
    return out


# Lo que el aprovisionamiento otorga con la bandera encendida TIENE que pasar la sonda con la
# bandera encendida, y NO pasarla con la bandera apagada: si divergen, o nada verifica o la
# bandera no se hace cumplir. Se detecta en el import y no en producción.
_PROC_GRANT_SHOW_GRANTS_LINE = f"GRANT SELECT ON {MYSQL_PROC_TABLE} TO `u`@`%`"
assert mysql_grant_violations([_PROC_GRANT_SHOW_GRANTS_LINE], allow_mysql_proc=True) == []
assert mysql_grant_violations([_PROC_GRANT_SHOW_GRANTS_LINE], allow_mysql_proc=False) == [
    "select_on_mysql_schema"
]
assert mysql_grant_violations(["GRANT SELECT ON mysql.* TO `u`@`%`"], allow_mysql_proc=True) == [
    "select_on_mysql_schema"
]


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


# --------------------------------------------------------------------------- #
# Sonda de la credencial de DATOS por base (design D18): SELECT sobre EXACTAMENTE una base          #
# --------------------------------------------------------------------------- #

#: Privilegios de ESTRUCTURA que la credencial de datos NO recibe, pero que no escriben filas: si
#: aparecen son exceso (``extra_privilege``), no escritura. ``TRIGGER``/``EVENT`` quedan fuera a
#: propósito: permiten crear objetos, y para la cuenta de datos sí cuentan como escritura.
_MYSQL_DATA_EXTRA_PRIVILEGES = MYSQL_ALLOWED_PRIVILEGES - {"USAGE", "SELECT", "TRIGGER", "EVENT"}

#: Motores que reenvían consultas a OTRO servidor: un ``SELECT`` sobre una tabla así sale de la
#: base y cruza el límite del grant (BLOQUEANTE, spec S32 enmendada; D18).
MYSQL_FOREIGN_ENGINES = ("FEDERATED", "CONNECT", "SPIDER")
POSTGRES_FOREIGN_EXTENSIONS = ("dblink", "postgres_fdw", "mysql_fdw", "file_fdw")

#: Tope de conexiones simultáneas con el que se aprovisiona el rol de PostgreSQL.
POSTGRES_DATA_MAX_CONNECTIONS = 3


def _split_grant_object(obj: str) -> list[tuple[str, bool]] | None:
    """
    ``\\`app\\_prod\\`.*`` → ``[("app\\_prod", True), ("*", False)]`` (texto, venía entre comillas).

    ``None`` si la forma no se entiende: el llamador la reporta como ``unrecognized_grant``.
    Un ``*`` entre comillas es un nombre, no el comodín: por eso se conserva el flag.
    """
    parts: list[tuple[str, bool]] = []
    i, n = 0, len(obj)
    while True:
        if i < n and obj[i] == "`":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    return None
                if obj[j] == "`":
                    if j + 1 < n and obj[j + 1] == "`":
                        buf.append("`")
                        j += 2
                        continue
                    break
                buf.append(obj[j])
                j += 1
            parts.append(("".join(buf), True))
            i = j + 1
        else:
            j = obj.find(".", i)
            j = n if j == -1 else j
            parts.append((obj[i:j], False))
            i = j
        if i >= n:
            break
        if obj[i] != ".":
            return None
        i += 1
    return parts


def _unescape_db_pattern(pattern: str) -> tuple[str, bool]:
    """
    Nombre real de un patrón de base de ``SHOW GRANTS`` y si trae comodines SIN escapar.

    ``app\\_prod`` → ``("app_prod", False)``; ``app_prod`` → ``("app_prod", True)``: un ``_`` o
    un ``%`` sin barra cubren OTRAS bases (``appXprod``), y eso es lo que la sonda busca.
    """
    out: list[str] = []
    wildcard = False
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            out.append(pattern[i + 1])
            i += 2
            continue
        if ch in "_%":
            wildcard = True
        out.append(ch)
        i += 1
    return "".join(out), wildcard


def mysql_data_grant_violations(
    lines: list[str], *, database: str, lower_case_table_names: int | None = 0
) -> list[str]:
    """
    Motivos por los que ``SHOW GRANTS FOR CURRENT_USER()`` NO es "SELECT sobre exactamente
    ``database`` y nada más". PURA: allowlist, y lo que no se entiende es una violación.

    Admite ``USAGE ON *.*`` y ``SELECT`` (de base, tabla o columna) sobre ``database``. El
    nombre se compara carácter a carácter tras des-escapar el patrón; distingue mayúsculas salvo
    que el motor guarde los nombres en minúsculas (``lower_case_table_names`` 1 o 2). Un valor
    ausente cuenta como 0 (estricto): ante la duda no se asume el motor permisivo.
    """
    out: list[str] = []

    def add(code: str) -> None:
        if code not in out:
            out.append(code)

    insensitive = lower_case_table_names in (1, 2)
    target = database.lower() if insensitive else database
    has_select = False
    for line in lines:
        texto = (line or "").strip()
        if not texto:
            continue
        if "WITH GRANT OPTION" in texto.upper():
            add("grant_option")
        m = _GRANT_RE.match(texto)
        if m is None or re.match(r"^GRANT\s+PROXY\b", texto, re.IGNORECASE):
            add("unrecognized_grant")  # rol otorgado, PROXY, o una forma desconocida
            continue
        parts = _split_grant_object(m.group("obj"))
        if parts is None or len(parts) != 2:
            add("unrecognized_grant")
            continue
        (schema, schema_quoted), (table, table_quoted) = parts
        is_global = schema == "*" and not schema_quoted
        if is_global and (table != "*" or table_quoted):
            add("unrecognized_grant")
            continue
        for priv in _split_privileges(m.group("privs")):
            if priv == "USAGE":
                continue
            if is_global:
                add(f"global_privilege:{priv.lower().replace(' ', '_')}")
            if priv in ("ALL", "ALL PRIVILEGES"):
                add("all_privileges")
            elif priv in _MYSQL_DATA_EXTRA_PRIVILEGES:
                add(f"extra_privilege:{priv.lower().replace(' ', '_')}")
            elif priv != "SELECT":
                add(f"privilege:{priv.lower().replace(' ', '_')}")
            if is_global:
                continue
            name, wildcard = _unescape_db_pattern(schema)
            if wildcard:
                add("wildcard_database_pattern")
            elif (name.lower() if insensitive else name) != target:
                add("select_outside_database")
            elif priv == "SELECT":
                has_select = True
    if not has_select:
        add("missing_select_on_database")
    return out


def postgres_data_role_violations(facts: dict) -> tuple[list[str], list[str]]:
    """
    ``(violaciones, advertencias)`` del rol de datos de PostgreSQL a partir de los hechos que lee
    el adapter. Una clave AUSENTE de las que protegen cuenta como violación: si el adapter no
    pudo leer un hecho, la sonda no lo asume favorable.

    ``CREATE`` sobre la base o el esquema ``public`` es solo ADVERTENCIA: en PostgreSQL <= 14
    ``PUBLIC`` lo tiene por default y bloquear ahí dejaría la credencial inusable en un servidor
    estándar. Lo cierra ``default_transaction_read_only`` (que SÍ es bloqueante) y el intento de
    escritura real.
    """
    out: list[str] = []
    warnings: list[str] = []

    def add(code: str) -> None:
        if code not in out:
            out.append(code)

    for attr in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"):
        if facts.get(attr, True):
            add(f"role_attribute:{attr}")
    if facts.get("default_transaction_read_only") != "on":
        add("default_transaction_read_only_off")
    if facts.get("temp_write_succeeded", True):
        add("write_attempt_succeeded")
    if facts.get("table_write_privileges", 1):
        add("table_write_privileges")
    for role in facts.get("write_roles") or []:
        add(f"member_of:{role}")
    if facts.get("role_memberships", 1):
        add("member_of_role")
    if facts.get("foreign_access_extensions") or []:
        add("foreign_access_extension")
    if facts.get("explicit_connect_other_databases", 1):
        add("select_outside_database")
    limit = facts.get("rolconnlimit")
    if not isinstance(limit, int) or not 1 <= limit <= POSTGRES_DATA_MAX_CONNECTIONS:
        add("connection_limit")
    if str(facts.get("statement_timeout") or "0").strip() in ("0", "0ms", "0s", ""):
        add("statement_timeout_unset")
    if facts.get("can_create_in_database"):
        warnings.append("create_on_database")
    if facts.get("can_create_in_public"):
        warnings.append("create_on_schema_public")
    if facts.get("public_connect_other_databases"):
        warnings.append("public_connect_other_databases")
    return out, warnings


def data_credential_probe(
    dialect: str, facts: dict, *, database: str
) -> tuple[list[str], list[str]]:
    """
    Veredicto PURO de la sonda de datos: ``(violaciones bloqueantes, advertencias)``. Vacío de
    violaciones = verde. Un motor sin sonda o unos hechos que declaran ``engine_unsupported``
    nunca verifican (fail-closed, igual que ``ServerAdapter.readonly_violations``).
    """
    if facts.get("engine_unsupported"):
        return ["engine_unsupported"], []
    if dialect in ("mysql", "mariadb"):
        violations = mysql_data_grant_violations(
            facts.get("grants") or [],
            database=database,
            lower_case_table_names=facts.get("lower_case_table_names", 0),
        )
        if facts.get("foreign_engine_tables", 0):
            violations.append("foreign_engine_table")
        warnings: list[str] = []
        if facts.get("cross_schema_views", 0):
            warnings.append("cross_schema_view_reference")
        if facts.get("definer_views", 0):
            warnings.append("definer_views_present")
        return violations, warnings
    if dialect == "postgresql":
        return postgres_data_role_violations(facts)
    return ["engine_unsupported"], []


def data_probe_is_fresh(verified_at, *, now, max_age_days: int) -> bool:
    """
    ¿La última sonda verde es lo bastante reciente? ``verified_at`` ausente = no. PURA (``now``
    viene del llamador). Fecha futura (reloj corrido) = no fresca: ante la duda, se re-sonda.
    """
    if verified_at is None:
        return False
    age = (now - verified_at).total_seconds()
    return 0 <= age <= max_age_days * 86400


__all__ = [
    "MYSQL_FOREIGN_ENGINES",
    "POSTGRES_FOREIGN_EXTENSIONS",
    "POSTGRES_DATA_MAX_CONNECTIONS",
    "data_credential_probe",
    "data_probe_is_fresh",
    "mysql_data_grant_violations",
    "postgres_data_role_violations",
    "MYSQL_SHOW_ROUTINE_MIN_VERSION",
    "ReadonlyPreflight",
    "mysql_global_grants_for_version",
    "mysql_has_unrecognized_grants",
    "MYSQL_ALLOWED_PRIVILEGES",
    "MYSQL_READONLY_DB_GRANTS",
    "MYSQL_READONLY_GLOBAL_GRANTS",
    "mysql_grant_violations",
    "is_mariadb_engine",
    "MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE",
    "MARIADB_READONLY_DB_EXTRA_GRANTS",
    "MYSQL_PROC_TABLE",
    "proc_grant_supported",
    "postgres_role_violations",
]
