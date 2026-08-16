"""
Política de seguridad de la CONSOLA SQL — módulo **PURO** (sin motor, sin I/O, sin BD).

Clasifica un texto SQL arbitrario en un ``QueryPlan`` con un nivel de peligro por
sentencia. Es la única fuente de verdad de "qué se puede ejecutar directo, qué exige
confirmación y qué está prohibido". Al no tocar el motor es 100% testeable, igual que
``schema_diff`` y ``plan_integrity``.

Niveles (``READ`` < ``WRITE`` < ``DDL`` < ``BLOCKED``):

- ``read``    — se ejecuta directo, pero SIEMPRE dentro de una transacción READ ONLY
                (ver ``query_runner``): la clasificación estática no puede garantizar que
                un ``SELECT fn()`` no escriba, así que la garantía la da el MOTOR.
- ``write``   — DML (INSERT/UPDATE/DELETE/MERGE/REPLACE) y todo lo que toma locks.
                Exige ``confirm_token`` + ``confirm_target_name``.
- ``ddl``     — CREATE/ALTER/DROP/TRUNCATE/RENAME y todo lo OPACO (ver fail-closed).
                Exige confirmación igual que ``write``; se separa para que la UI pueda
                advertir distinto y para la auditoría.
- ``blocked`` — prohibido **incluso confirmando** mientras no exista un segundo factor.
                Ver ``_BLOCKLIST``.

DOS DECISIONES QUE EXPLICAN TODO EL DISEÑO
------------------------------------------

**1. Se clasifica por AST, no por palabras clave.** Un ``sql.upper().contains("DELETE")``
falla en ambas direcciones: marca ``SELECT * FROM logs WHERE accion = 'DELETE'`` y deja
pasar ``/*x*/ dElEtE FROM t``, ``WITH x AS (…) DELETE FROM t`` o ``CALL sp_borrar_todo()``.
Se parsea con sqlglot (ya es dependencia del proyecto) y se toma el **máximo** peligro de
CUALQUIER nodo del árbol, no solo de la raíz — si no,
``WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d`` (PostgreSQL) tiene raíz ``Select``
y pasaría como lectura. Verificado con el parser real: esa consulta expone raíz ``Select``
con un nodo ``Delete`` adentro.

**2. Fail-closed en TODOS los bordes.** Si sqlglot no puede parsear, si el tipo de nodo no
está mapeado, o si la sentencia es OPACA (``CALL``, ``DO``, bloque anónimo, ``PREPARE``/
``EXECUTE``), el resultado es PELIGROSO, nunca lectura. sqlglot degrada a un nodo genérico
``exp.Command`` para mucho más de lo que parece (verificado: ``GRANT``, ``CREATE USER``,
``ALTER SYSTEM``, ``CREATE EXTENSION``, ``REPLACE INTO``, ``DO``, ``SET ROLE``, ``CALL``,
``RENAME TABLE``, ``EXPLAIN ANALYZE``), así que ``Command`` ⇒ peligroso por definición.

POR QUÉ LA BLOCKLIST DE TEXTO NO ES REDUNDANTE
-----------------------------------------------
El AST NO alcanza para lo prohibido. Verificado contra el parser real: ``FLUSH PRIVILEGES``
parsea como un ``exp.Alias`` (indistinguible de una expresión inofensiva) y todo el DCL cae
en ``exp.Command`` sin estructura que inspeccionar. Por eso la blocklist corre sobre el
texto NORMALIZADO (``_scan_normalize``: sin comentarios, con el contenido de los literales
vaciado) ANTES de parsear, y su veredicto es terminal.

Vaciar los literales evita el falso positivo (``WHERE accion = 'GRANT'`` no bloquea) y
quitar los comentarios evita la evasión (``/*x*/GRANT``). Los comentarios ejecutables de
MySQL (``/*!40101 … */``) se conservan como CÓDIGO a propósito: son sentencias reales.
"""

import hashlib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.services.db_admin.identifiers import references_gateway_internal_table
from app.services.db_admin.sql_dialect import split_sql_statements

# --------------------------------------------------------------------------- #
# Niveles                                                                      #
# --------------------------------------------------------------------------- #

READ = "read"
WRITE = "write"
DDL = "ddl"
BLOCKED = "blocked"

_RANK = {READ: 0, WRITE: 1, DDL: 2, BLOCKED: 3}

# Dialecto de negocio -> dialecto de sqlglot.
_SQLGLOT_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres"}


def worst(*levels: str) -> str:
    """Nivel más severo de los recibidos (READ si no hay ninguno)."""
    return max(levels, key=lambda lv: _RANK.get(lv, _RANK[DDL]), default=READ)


@dataclass(frozen=True)
class Reason:
    """Motivo legible de una clasificación. ``code`` es estable para el frontend."""

    code: str
    message: str


@dataclass(frozen=True)
class StatementPlan:
    seq: int
    sql: str
    kind: str
    danger: str
    reasons: tuple[Reason, ...] = ()
    # SELECT COUNT(*) derivado del UPDATE/DELETE, para estimar el impacto ANTES de
    # confirmar. None => no se pudo derivar de forma EXACTA (ver _impact_query).
    impact_query: str | None = None
    # La MISMA consulta con un ``LIMIT`` empujado al motor. None => no se puede acotar
    # sin cambiar la semántica (ver ``_limited_sql``); ahí el tope se aplica del lado del
    # gateway, que acota la memoria pero no el transporte.
    fetch_sql: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.danger == BLOCKED


@dataclass(frozen=True)
class QueryPlan:
    statements: tuple[StatementPlan, ...]
    danger: str
    sql_hash: str
    reasons: tuple[Reason, ...] = field(default=())

    @property
    def is_blocked(self) -> bool:
        return self.danger == BLOCKED

    @property
    def requires_confirmation(self) -> bool:
        """Todo lo que no sea lectura pura exige confirmación en modo seguro."""
        return _RANK.get(self.danger, _RANK[BLOCKED]) >= _RANK[WRITE]

    @property
    def blocked_statements(self) -> tuple[StatementPlan, ...]:
        return tuple(s for s in self.statements if s.is_blocked)


# --------------------------------------------------------------------------- #
# Normalización para el escaneo de texto                                       #
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")

# Prefijos de comentario EJECUTABLE de la familia MySQL, del más largo al más corto para
# que ``/*M!`` no se confunda nunca con ``/*``. ``/*!`` lo ejecutan MySQL y MariaDB;
# ``/*M!`` es exclusivo de MariaDB (y su ``M`` es sensible a mayúsculas en el motor, pero
# acá se acepta también minúscula: reconocer de más solo sobre-bloquea).
_EXECUTABLE_COMMENT_PREFIXES = ("/*M!", "/*m!", "/*!")


def _executable_comment_prefix(sql: str, i: int) -> int:
    """
    Largo del prefijo de comentario ejecutable que abre en ``i``, o ``0`` si no hay.

    Devolver el LARGO y no un booleano es lo que permite que el llamador salte el prefijo
    correcto (3 para ``/*!``, 4 para ``/*M!``) sin duplicar la tabla de prefijos.
    """
    for prefix in _EXECUTABLE_COMMENT_PREFIXES:
        if sql.startswith(prefix, i):
            return len(prefix)
    return 0


def _scan_normalize(sql: str, *, engine: str = "mysql") -> str:
    """
    SQL en MAYÚSCULAS, sin comentarios, con el CONTENIDO de los literales de cadena
    vaciado y el espaciado colapsado. Es la entrada de la blocklist.

    - Literales ``'…'`` => ``''``. Una palabra clave DENTRO de un literal no se ejecuta,
      así que no debe disparar la blocklist.
    - Comentarios ``--`` y ``/* … */`` se eliminan; ``#`` **solo en MySQL/MariaDB** (ver
      abajo)… salvo ``/*! … */`` de MySQL/MariaDB, que el motor SÍ ejecuta: su contenido
      se conserva como código.
    - Identificadores citados (backticks / comillas dobles) se CONSERVAN: no son
      ejecutables por sí mismos y perderlos ocultaría el objeto afectado.
    - Bloques con dollar-quoting de PostgreSQL (``$tag$ … $tag$``) se conservan enteros:
      son el CUERPO de una rutina, y una blocklist que los ignorara dejaría pasar un
      ``COPY … FROM PROGRAM`` escondido en un ``CREATE FUNCTION``.

    DOS REGLAS QUE PARECEN DETALLES Y SON AGUJEROS DE SEGURIDAD:

    **``#`` NO es un comentario en PostgreSQL** — es el operador XOR de enteros. Tratarlo
    como comentario en los tres motores BORRABA código ejecutable del texto que escanea la
    blocklist: ``SELECT id # 0, lo_import('/etc/shadow') FROM t`` quedaba en ``SELECT ID``
    y se clasificaba ``read``, es decir, se ejecutaba sin confirmación ni auditoría. Por eso
    la función necesita saber el motor.

    **La barra invertida NO escapa comillas aquí**, a propósito, aunque MySQL sí la trate
    así por defecto. Es el mismo criterio que usa ``split_sql_statements``, y tener las dos
    capas de acuerdo importa más que imitar al motor: si esta función "cerrara" un literal
    que el splitter dejó abierto, habría texto ejecutable que ninguna de las dos ve. Al no
    escapar, un ``'\\'; DROP DATABASE x'`` termina con la segunda mitad FUERA del literal y
    la blocklist la ve — fail-closed. El costo es sobre-bloquear alguna consulta con
    apóstrofos escapados, no dejar pasar una peligrosa.
    """
    hash_is_comment = engine in ("mysql", "mariadb")
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        # --- comentarios ---
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
            continue
        if ch == "#" and hash_is_comment:
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
            continue
        if ch == "/" and sql.startswith("/*", i):
            executable = _executable_comment_prefix(sql, i)
            if executable:
                # Comentario EJECUTABLE de MySQL/MariaDB: se conserva el contenido como
                # código. El número que sigue al prefijo es la versión MÍNIMA del motor
                # (``/*!40101 GRANT … */``), no parte de la sentencia: si no se descarta,
                # queda como primera palabra y los patrones anclados con ``^`` no matchean
                # — es decir, sería una evasión trivial de la blocklist.
                #
                # ``/*M!`` es la variante EXCLUSIVA de MariaDB (``/*M!100000 … */``) y su
                # ausencia acá era un agujero real: el contenido se descartaba como
                # comentario común, la blocklist nunca lo veía y el motor lo ejecutaba
                # igual. Con la credencial pseudo-root, un
                # ``/*M!100000 INTO OUTFILE '/tmp/x' */`` es escritura de archivo
                # arbitraria en el host de la base del cliente. Se reconoce en los TRES
                # motores a propósito (fail-closed): un MariaDB dado de alta como ``mysql``
                # es un error de inventario frecuente, y conservar texto de más solo puede
                # sobre-bloquear, nunca dejar pasar.
                j = sql.find("*/", i + executable)
                body = re.sub(r"^\d+", "", sql[i + executable : (n if j == -1 else j)])
                out.append(" " + body + " ")
                i = n if j == -1 else j + 2
                continue
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue

        # --- literales de cadena: se vacían ---
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # '' escapada
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
            continue

        # --- identificadores citados: se conservan tal cual ---
        if ch in ('"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # --- dollar-quoting de PostgreSQL: se conserva entero ---
        if ch == "$":
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                end = n if j == -1 else j + len(tag)
                out.append(sql[i:end])
                i = end
                continue

        out.append(ch)
        i += 1

    return _WS_RE.sub(" ", "".join(out)).strip().upper()


# --------------------------------------------------------------------------- #
# Blocklist — prohibido INCLUSO confirmando (hasta que exista un 2º factor)     #
# --------------------------------------------------------------------------- #

# Cada entrada: (code, regex sobre el texto normalizado, mensaje).
# Las que solo son válidas como PRIMERA palabra van ancladas con ``^`` para no
# marcar una tabla/columna que se llame igual.
_BLOCKLIST: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # --- DCL: evita por completo el módulo de permisos del gateway y su auditoría ---
    (
        "dcl_grant_revoke",
        re.compile(r"^(GRANT|REVOKE)\b"),
        "GRANT/REVOKE no se ejecutan desde la consola: usa los endpoints de privilegios, "
        "que aplican los guards anti-lockout y dejan auditoría estructurada.",
    ),
    (
        "dcl_user_role",
        re.compile(
            r"^(CREATE|ALTER|DROP)\s+(OR\s+REPLACE\s+)?(USER|ROLE|GROUP)\b"
            r"|^RENAME\s+USER\b|^SET\s+PASSWORD\b"
        ),
        "La gestión de usuarios/roles no se ejecuta desde la consola: usa los endpoints "
        "de usuarios del motor (cifran la credencial y auditan la operación).",
    ),
    # --- Acceso a archivos del host de la BD (≈ ejecución remota con pseudo-root) ---
    (
        "server_file_access",
        re.compile(
            r"\b(FROM|TO)\s+PROGRAM\b"
            r"|\bINTO\s+(OUTFILE|DUMPFILE)\b"
            r"|^LOAD\s+DATA\b"
            r"|\b(LOAD_FILE|LO_IMPORT|LO_EXPORT|PG_READ_FILE|PG_READ_BINARY_FILE"
            r"|PG_LS_DIR|PG_STAT_FILE)\s*\("
        ),
        "La sentencia accede al sistema de archivos del servidor de base de datos. "
        "El gateway conecta con una credencial pseudo-root, así que el motor SÍ lo "
        "permitiría; está prohibido por política.",
    ),
    (
        "copy_statement",
        re.compile(r"^COPY\b"),
        "COPY lee/escribe archivos en el host del servidor. Para mover datos entre bases "
        "usa el módulo de clonado, que lo hace por streaming y con auditoría.",
    ),
    (
        "extension_or_untrusted_language",
        re.compile(
            r"^CREATE\s+(OR\s+REPLACE\s+)?EXTENSION\b"
            r"|\bLANGUAGE\s+(C|PLPYTHON\w*|PLPERLU|PLTCLU)\b"
        ),
        "Instalar extensiones o crear rutinas en un lenguaje no confiable permite "
        "ejecutar código arbitrario en el host del servidor.",
    ),
    (
        "native_code_load",
        re.compile(
            r"^(INSTALL|UNINSTALL)\s+SONAME\b"
            # ``CREATE [AGGREGATE] FUNCTION … SONAME 'lib.so'`` es la UDF de MySQL/MariaDB:
            # carga una librería NATIVA. Es la vía clásica de ejecución de comandos del SO,
            # y el equivalente de PostgreSQL (``LANGUAGE C``) ya estaba cubierto.
            r"|\bSONAME\b"
            # ``CREATE LANGUAGE`` registra un handler arbitrario y esquiva la lista de
            # nombres conocidos de ``extension_or_untrusted_language``.
            r"|^CREATE\s+(OR\s+REPLACE\s+)?(TRUSTED\s+|PROCEDURAL\s+)*LANGUAGE\b"
        ),
        "Cargar una librería nativa (UDF/plugin) o registrar un lenguaje procedural "
        "ejecuta código arbitrario en el host del servidor de base de datos.",
    ),
    (
        "outbound_connection",
        re.compile(
            r"^(CREATE|ALTER|DROP)\s+SERVER\b"
            r"|^CREATE\s+(OR\s+REPLACE\s+)?FOREIGN\s+DATA\s+WRAPPER\b"
            r"|^IMPORT\s+FOREIGN\s+SCHEMA\b"
            r"|^(CREATE|ALTER|DROP)\s+(PUBLICATION|SUBSCRIPTION)\b"
            r"|\bDBLINK\w*\s*\("
            r"|\bENGINE\s*=\s*FEDERATED\b"
        ),
        "La sentencia hace que el SERVIDOR de base de datos abra una conexión saliente. "
        "Esa conexión la inicia el motor, así que NO pasa por el guard anti-SSRF del "
        "gateway.",
    ),
    (
        "server_control_function",
        re.compile(
            r"\bPG_(TERMINATE_BACKEND|CANCEL_BACKEND|RELOAD_CONF|ROTATE_LOGFILE|PROMOTE"
            r"|SWITCH_WAL|CREATE_RESTORE_POINT|DROP_REPLICATION_SLOT"
            r"|CREATE_(PHYSICAL|LOGICAL)_REPLICATION_SLOT)\s*\("
        ),
        "La función administra el servidor entero (mata sesiones, recarga configuración, "
        "promueve el standby). No es una lectura por más que viaje dentro de un SELECT, y "
        "una transacción de solo lectura no la frena.",
    ),
    # --- Estado GLOBAL del servidor: afecta a todas las BDs, no solo a la elegida ---
    (
        "server_global_state",
        re.compile(
            r"^SET\s+(GLOBAL|PERSIST|PERSIST_ONLY)\b"
            # ``SET @@GLOBAL.x = …`` es la misma operación con otra sintaxis.
            r"|^SET\s+@@\s*(GLOBAL|PERSIST|PERSIST_ONLY)\s*\."
            r"|^ALTER\s+SYSTEM\b"
            r"|^(FLUSH|SHUTDOWN|KILL|RESET|RESTART|BINLOG|PURGE|CLONE)\b"
            r"|^ALTER\s+INSTANCE\b"
            r"|^(CACHE\s+INDEX|LOAD\s+INDEX)\b"
            r"|^(START|STOP)\s+(SLAVE|REPLICA)\b"
            r"|^CHANGE\s+(MASTER|REPLICATION)\b"
            r"|^(INSTALL|UNINSTALL)\s+(PLUGIN|COMPONENT)\b"
            r"|^(CREATE|DROP|ALTER)\s+TABLESPACE\b"
        ),
        "La sentencia modifica el estado GLOBAL del servidor y afectaría a todas sus "
        "bases de datos, no solo a la seleccionada.",
    ),
    # --- Ciclo de vida de BDs: tiene endpoint dedicado con doble confirmación ---
    (
        "database_lifecycle",
        re.compile(r"^(CREATE|DROP|ALTER)\s+(DATABASE|SCHEMA)\b"),
        "Crear, modificar o eliminar bases de datos/esquemas tiene endpoints dedicados "
        "con confirmación por nombre, token firmado y guard de BDs de sistema.",
    ),
    (
        # Las garantías de la consola (solo lectura, timeout, a qué tabla apunta un
        # nombre sin calificar) son parámetros de SESIÓN. Si el SQL del usuario puede
        # cambiarlos, las garantías dejan de valer para las sentencias siguientes del
        # mismo lote. ``SET search_path`` es el más traicionero: el COUNT del preview
        # corre en OTRA conexión sin ese cambio, así que la estimación contaría
        # ``public.t`` mientras el DELETE confirmado golpea ``otro.t``.
        "session_guarantee_override",
        re.compile(
            r"^SET\s+(SESSION\s+|LOCAL\s+|@@(SESSION|LOCAL)?\.?)?"
            r"(STATEMENT_TIMEOUT|LOCK_TIMEOUT|IDLE_IN_TRANSACTION_SESSION_TIMEOUT"
            r"|MAX_EXECUTION_TIME|MAX_STATEMENT_TIME|SEARCH_PATH"
            r"|DEFAULT_TRANSACTION_READ_ONLY|TRANSACTION_READ_ONLY"
            r"|FOREIGN_KEY_CHECKS|UNIQUE_CHECKS|SQL_LOG_BIN|SQL_MODE"
            r"|SESSION_REPLICATION_ROLE)\b"
            r"|^SET\s+STATEMENT\b"
            r"|^XA\b"
        ),
        "La sentencia cambia parámetros de sesión que sostienen las garantías de la "
        "consola (timeout, solo lectura, o el esquema con el que se resuelven los "
        "nombres sin calificar).",
    ),
    # --- Control de sesión/transacción: rompería el envoltorio del propio runner ---
    (
        "session_control",
        re.compile(
            r"^(BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK|SAVEPOINT"
            r"|RELEASE\s+SAVEPOINT|SET\s+TRANSACTION|SET\s+CONSTRAINTS)\b"
            r"|^SET\s+AUTOCOMMIT\b"
            r"|^(LOCK|UNLOCK)\s+TABLES\b"
            r"|^USE\b"
            r"|^HANDLER\b"
        ),
        "La consola administra su propia sesión y transacción; las sentencias de control "
        "de sesión/transacción romperían esa garantía (incluida la de solo lectura).",
    ),
    (
        # Un superusuario de PostgreSQL puede volver a serlo con cualquiera de estas
        # formas, así que todas anulan el modo ``impersonate``. ``set_config`` hay que
        # bloquearla ENTERA: ``_scan_normalize`` vacía los literales, así que llega como
        # ``SET_CONFIG('','',FALSE)`` y es imposible distinguir por su primer argumento.
        "role_switch",
        re.compile(
            r"^(SET|RESET)\s+(SESSION\s+|LOCAL\s+)?ROLE\b"
            r"|^(SET|RESET)\s+(SESSION\s+|LOCAL\s+)?SESSION\s+AUTHORIZATION\b"
            r"|^SET\s+LOCAL\s+SESSION\s+AUTHORIZATION\b"
            r"|\bSET_CONFIG\s*\("
            r"|^DISCARD\b"
        ),
        "Cambiar de rol dentro de la consola anularía el usuario elegido para la prueba "
        "de permisos y devolvería la sesión a la credencial pseudo-root. Elegí el usuario "
        "en el propio request.",
    ),
    (
        # ``DELIMITER`` es una directiva del CLIENTE mysql, no del motor. Aquí su único
        # efecto es agrupar varias sentencias del servidor en una sola unidad del
        # splitter, y con eso el keyword peligroso deja de estar al principio del texto
        # normalizado: TODA la blocklist anclada con ``^`` se evadía con
        # ``DELIMITER //`` + ``UPDATE t SET x=1; GRANT ALL …``. No hace falta para nada:
        # el splitter reconoce los cuerpos ``BEGIN…END`` de las rutinas por sí solo.
        "delimiter_directive",
        re.compile(r"^DELIMITER\b"),
        "La directiva DELIMITER no se admite en la consola: es del cliente mysql, no del "
        "motor, y agrupar sentencias con ella impediría clasificarlas una por una. Enviá "
        "las sentencias separadas por ';' — los cuerpos BEGIN…END se reconocen solos.",
    ),
    # --- SQL dinámico: ejecutaría texto que esta política nunca llegó a clasificar ---
    (
        "dynamic_sql",
        re.compile(r"^(PREPARE|EXECUTE|DEALLOCATE)\b"),
        "Las sentencias preparadas ejecutan SQL que esta política no puede clasificar de "
        "antemano.",
    ),
)

# Esquemas del sistema: leerlos es legítimo (probar permisos), ESCRIBIRLOS no.
_SYSTEM_SCHEMA_RE = re.compile(
    r"\b(MYSQL|INFORMATION_SCHEMA|PERFORMANCE_SCHEMA|SYS|PG_CATALOG|PG_TOAST"
    r"|PG_TEMP\w*|PG_TOAST_TEMP\w*)\s*\."
)

# Señales de PELIGRO que viven en el TEXTO y que el AST puede no ver. sqlglot no
# tokeniza el contenido de un ``/*! … */``, así que ``SELECT * FROM t /*!40101 FOR
# UPDATE*/`` salía ``read`` pese a tomar locks. Estos patrones ELEVAN el nivel (no
# bloquean): son el respaldo textual de lo que ``_classify_ast`` detecta por nodo.
_TEXT_ELEVATORS: tuple[tuple[str, re.Pattern[str], str, str], ...] = (
    (
        WRITE,
        re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE)\b|\bLOCK\s+IN\s+SHARE\s+MODE\b"),
        "row_locking_read",
        "La consulta bloquea filas (FOR UPDATE / FOR SHARE); no es una lectura pura.",
    ),
    (
        # Solo la forma ``INTO @variable`` de MySQL/MariaDB: ``INTO OUTFILE``/``DUMPFILE``
        # ya los bloquea ``server_file_access``, y el ``SELECT … INTO tabla`` de
        # PostgreSQL lo ve el AST (PostgreSQL no tiene comentarios ejecutables, así que
        # ahí no hay nada que se le pueda esconder). Sin este respaldo,
        # ``SELECT 1 /*!40100 INTO @x*/`` salía ``read``.
        DDL,
        re.compile(r"\bINTO\s+@"),
        "select_into",
        "SELECT … INTO asigna una variable de sesión; no es una lectura pura.",
    ),
)


# --------------------------------------------------------------------------- #
# Mapeo AST -> nivel                                                           #
# --------------------------------------------------------------------------- #

_READ_ROOTS = (exp.Select, exp.Union, exp.Show, exp.Describe, exp.Pragma)
_WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
_DDL_NODES = (exp.Create, exp.Alter, exp.Drop, exp.TruncateTable, exp.Analyze)

_KIND_BY_NODE = {
    exp.Select: "select",
    exp.Union: "select",
    exp.Show: "show",
    exp.Describe: "describe",
    exp.Insert: "insert",
    exp.Update: "update",
    exp.Delete: "delete",
    exp.Merge: "merge",
    exp.Create: "create",
    exp.Alter: "alter",
    exp.Drop: "drop",
    exp.TruncateTable: "truncate",
    exp.Analyze: "analyze",
    exp.Set: "set",
    exp.Copy: "copy",
    exp.Use: "use",
    exp.Kill: "kill",
    exp.Command: "unknown",
}

# Sentencias que fallan el parseo de sqlglot pero son de LECTURA sin ambigüedad. Es
# seguro admitirlas porque el nivel ``read`` se ejecuta dentro de una transacción READ
# ONLY: si el reconocimiento se equivocara, el motor aborta la sentencia.
# ``EXPLAIN ANALYZE`` queda FUERA a propósito: en PostgreSQL EJECUTA la consulta.
_READ_FALLBACK_RE = re.compile(r"^(SHOW|DESC|DESCRIBE|EXPLAIN)\b(?!.*\bANALYZE\b)")

# ``EXPLAIN ANALYZE`` **EJECUTA** la consulta que analiza (en los tres motores), así que
# un ``EXPLAIN ANALYZE DELETE …`` borra filas de verdad. sqlglot lo parsea como un
# ``exp.Describe`` — indistinguible de un ``EXPLAIN`` normal, que sí es lectura — por eso
# hace falta este chequeo explícito sobre el texto.
_EXECUTING_EXPLAIN_RE = re.compile(r"^(EXPLAIN|DESC|DESCRIBE)\b.*\bANALYZE\b")


def _sqlglot_dialect(engine: str) -> str:
    return _SQLGLOT_DIALECT.get(engine, "mysql")


def _classify_ast(tree: exp.Expression) -> tuple[str, str, list[Reason]]:
    """(danger, kind, reasons) a partir del árbol COMPLETO, no solo de la raíz."""
    reasons: list[Reason] = []
    danger = READ
    kind = _KIND_BY_NODE.get(type(tree), "unknown")

    for node in tree.walk():
        node_type = type(node)

        if isinstance(node, exp.Command):
            danger = worst(danger, DDL)
            reasons.append(
                Reason(
                    "opaque_statement",
                    "El parser no reconoce la estructura de la sentencia; se trata como "
                    "peligrosa por política fail-closed.",
                )
            )
            continue

        if isinstance(node, _WRITE_NODES):
            danger = worst(danger, WRITE)
            if node is not tree:
                reasons.append(
                    Reason(
                        "nested_dml",
                        f"La sentencia contiene un {_KIND_BY_NODE.get(node_type, 'DML')} "
                        "anidado (CTE o subconsulta) que modifica datos.",
                    )
                )
            continue

        if isinstance(node, _DDL_NODES):
            danger = worst(danger, DDL)
            continue

        if isinstance(node, exp.Lock):
            # SELECT … FOR UPDATE / FOR SHARE: toma locks de fila.
            danger = worst(danger, WRITE)
            reasons.append(
                Reason(
                    "row_locking_read",
                    "La consulta bloquea filas (FOR UPDATE / FOR SHARE); no es una "
                    "lectura pura.",
                )
            )
            continue

        if isinstance(node, exp.Set):
            danger = worst(danger, WRITE)
            continue

    # ``SELECT … INTO tabla`` (PostgreSQL) CREA una tabla; ``SELECT … INTO @var``
    # (MySQL) es estado de sesión. Ninguna de las dos es lectura.
    if tree.args.get("into") is not None:
        danger = worst(danger, DDL)
        reasons.append(
            Reason(
                "select_into",
                "SELECT … INTO materializa el resultado (crea una tabla o asigna una "
                "variable de sesión); no es una lectura pura.",
            )
        )

    if danger == READ and not isinstance(tree, _READ_ROOTS):
        # Tipo de raíz no mapeado: fail-closed.
        danger = DDL
        reasons.append(
            Reason(
                "unmapped_statement",
                "Tipo de sentencia no contemplado por la política; se trata como "
                "peligrosa por política fail-closed.",
            )
        )

    return danger, kind, reasons


# --------------------------------------------------------------------------- #
# Estimación de impacto (UPDATE/DELETE -> SELECT COUNT(*))                      #
# --------------------------------------------------------------------------- #


def _table_names(node: exp.Expression) -> set[str]:
    return {t.sql().upper() for t in node.find_all(exp.Table)}


def _impact_query(tree: exp.Expression, dialect: str) -> str | None:
    """
    ``SELECT COUNT(*)`` EXACTO para un UPDATE/DELETE de UNA sola tabla, o ``None``.

    Solo se emite cuando el conteo es **exacto**. Se descarta si hay más de una fuente
    de filas, porque el número sería engañoso justo donde más importa:

    - ``DELETE FROM t USING u WHERE t.id = u.id`` (PostgreSQL): el ``USING`` vive en otra
      rama del árbol y el COUNT ingenuo queda ``… FROM t WHERE t.id = u.id``, con ``u``
      fuera de alcance. Verificado con el generador real.
    - ``UPDATE a JOIN b …`` (MySQL): el COUNT del join cuenta filas del producto, que
      puede superar a las filas realmente actualizadas de ``a``.

    Un ``None`` NO relaja nada: la confirmación se sigue exigiendo, solo que sin cifra.
    """
    if not isinstance(tree, (exp.Update, exp.Delete)):
        return None

    target = tree.this
    if not isinstance(target, exp.Table):
        return None
    # Fuentes adicionales explícitas (joins de MySQL, USING/FROM de PostgreSQL).
    if target.args.get("joins") or tree.args.get("using") or tree.args.get("from"):
        return None
    if tree.args.get("joins"):
        return None

    where = tree.args.get("where")
    count = exp.select(exp.Count(this=exp.Star())).from_(target.copy())
    if where is not None and where.this is not None:
        count = count.where(where.this.copy())

    # Red de seguridad: si el COUNT no referencia EXACTAMENTE las mismas tablas que la
    # sentencia original, alguna fuente quedó fuera y el número sería falso.
    if _table_names(count) != _table_names(tree):
        return None

    try:
        return count.sql(dialect=dialect)
    except Exception:  # noqa: BLE001 — generar el COUNT nunca debe romper el preview
        return None


# --------------------------------------------------------------------------- #
# Tope de filas empujado al MOTOR                                              #
# --------------------------------------------------------------------------- #


def _limited_sql(tree: exp.Expression | None, dialect: str, max_rows: int | None) -> str | None:
    """
    La misma consulta con ``LIMIT max_rows + 1``, o ``None`` si no se puede acotar.

    POR QUÉ ES LA ÚNICA DEFENSA REAL. Recortar del lado del gateway
    (``fetchmany(max_rows + 1)`` + cerrar el cursor) acota la MEMORIA pero no el
    transporte: en MySQL/MariaDB, cerrar un cursor sin agotar dispara
    ``MySQLResult._finish_unbuffered_query()``, que —según el comentario del propio
    pymysql— gira leyendo paquetes hasta el EOF *porque no hay forma de que el servidor
    deje de mandarlos*. Es decir, ``SELECT * FROM tabla_de_50M`` con tope de 1000 filas
    transfería igual las 50M. Con el ``LIMIT`` en la sentencia, el servidor manda 1001
    filas y se acabó.

    Se pide una fila DE MÁS para poder informar ``truncated`` con certeza.

    NO se acota cuando cambiaría la semántica:
    - Solo raíces ``Select``/``Union``: el resto (``SHOW``, ``Command``, DDL) no admite
      ``LIMIT`` y devuelve pocas filas o ninguna.
    - Con ``FOR UPDATE``/``FOR SHARE``, el ``LIMIT`` cambia QUÉ FILAS se bloquean.
    - Con ``INTO``, el resultado se materializa en otro lado.
    - Un ``LIMIT`` propio más chico se respeta tal cual (``.limit()`` lo REEMPLAZARÍA).
    """
    if max_rows is None or tree is None:
        return None
    if not isinstance(tree, (exp.Select, exp.Union)):
        return None
    if tree.args.get("into") is not None:
        return None
    if any(isinstance(node, exp.Lock) for node in tree.walk()):
        return None

    existing = tree.args.get("limit")
    if existing is not None:
        literal = getattr(existing, "expression", None)
        if isinstance(literal, exp.Literal) and literal.is_int:
            try:
                if int(literal.name) <= max_rows:
                    return None
            except (TypeError, ValueError):
                return None
        else:
            # ``LIMIT ?`` o una expresión: no se puede comparar, se deja como está.
            return None

    try:
        return tree.copy().limit(max_rows + 1).sql(dialect=dialect)
    except Exception:  # noqa: BLE001 — acotar es una optimización, nunca rompe el plan
        return None


# --------------------------------------------------------------------------- #
# Clasificación                                                                #
# --------------------------------------------------------------------------- #


def _unquoted(scanned: str) -> str:
    """Variante sin comillas ni backticks, para patrones que exigen ``esquema.``."""
    return scanned.replace("`", "").replace('"', "")


def _blocklist_hits(scanned: str) -> list[Reason]:
    """
    Aplica la blocklist al texto completo **y a cada segmento entre ``;``**.

    Los patrones anclados con ``^`` asumen que el texto empieza donde empieza una
    sentencia del servidor. Dos construcciones rompen esa premisa y evadían TODA la
    blocklist anclada (DCL, estado global, ciclo de vida de BDs, SQL dinámico…):

    - ``DELIMITER //`` seguido de ``UPDATE t SET x=1; GRANT ALL …``: el splitter entrega
      las dos sentencias como UNA sola unidad, así que ``GRANT`` deja de estar al inicio.
    - ``SELECT 1/*!;DROP DATABASE victima*/``: el comentario EJECUTABLE de MySQL se
      conserva como código (bien), pero su contenido queda a mitad del texto.

    Escanear por segmento lo cierra, y es seguro porque ``_scan_normalize`` ya vació el
    contenido de los literales: un ``;`` dentro de una cadena no puede partir nada.

    Efecto colateral aceptado: el cuerpo de una rutina que contenga ``COMMIT;`` o
    ``START TRANSACTION;`` queda bloqueado. Es fail-closed y deseable — crear rutinas con
    control de transacción o DCL adentro desde una consola ad-hoc es justamente la vía de
    escalada que la blocklist existe para cortar (crear la rutina y después llamarla).
    """
    hits: list[Reason] = []
    seen: set[str] = set()
    for segment in [scanned, *scanned.split(";")]:
        seg = segment.strip()
        if not seg:
            continue
        for code, pattern, message in _BLOCKLIST:
            if code not in seen and pattern.search(seg):
                seen.add(code)
                hits.append(Reason(code, message))
    return hits


def classify_statement(
    sql: str, *, engine: str, seq: int = 0, max_rows: int | None = None
) -> StatementPlan:
    """Clasifica UNA sentencia ya separada del lote."""
    scanned = _scan_normalize(sql, engine=engine)

    if not scanned:
        return StatementPlan(seq=seq, sql=sql, kind="empty", danger=READ)

    # 1) Blocklist de texto: veredicto TERMINAL, corre antes de parsear (el AST no
    #    alcanza — ``FLUSH PRIVILEGES`` parsea como una expresión inofensiva).
    blocked = _blocklist_hits(scanned)

    # 2) Contabilidad INTERNA del gateway: la consola no puede ser una vía nueva para
    #    tocar ``_gw_v_*`` / ``_gw_stg_*`` (mismo guard que ya aplican las migraciones).
    internal = references_gateway_internal_table(sql)
    if internal:
        blocked.append(
            Reason(
                "gateway_internal_table",
                "La sentencia nombra tablas de contabilidad interna del gateway "
                f"({', '.join(sorted(set(internal)))}); modificarlas dejaría la base "
                "sin puntero de versión de migraciones.",
            )
        )

    if blocked:
        return StatementPlan(
            seq=seq, sql=sql, kind="blocked", danger=BLOCKED, reasons=tuple(blocked)
        )

    # 3) AST.
    dialect = _sqlglot_dialect(engine)
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — sqlglot lanza varias familias de error
        tree = None

    if tree is None:
        if _READ_FALLBACK_RE.match(scanned):
            return StatementPlan(
                seq=seq,
                sql=sql,
                kind="show",
                danger=READ,
                reasons=(
                    Reason(
                        "read_by_leading_keyword",
                        "El parser no reconoce la sentencia, pero su forma es de lectura. "
                        "Se ejecuta igualmente dentro de una transacción de solo lectura.",
                    ),
                ),
            )
        return StatementPlan(
            seq=seq,
            sql=sql,
            kind="unknown",
            danger=DDL,
            reasons=(
                Reason(
                    "unparseable",
                    "No se pudo analizar la sentencia; se trata como peligrosa por "
                    "política fail-closed.",
                ),
            ),
        )

    danger, kind, reasons = _classify_ast(tree)

    # 4) EXPLAIN ANALYZE ejecuta lo que analiza; no es lectura por más que lo parezca.
    if danger == READ and _EXECUTING_EXPLAIN_RE.match(scanned):
        danger = DDL
        reasons.append(
            Reason(
                "explain_analyze_executes",
                "EXPLAIN ANALYZE EJECUTA la sentencia analizada para medirla; no es una "
                "lectura. Usá EXPLAIN a secas para ver el plan sin ejecutar.",
            )
        )

    # 5) Respaldo TEXTUAL de lo que el AST puede no ver (contenido de un ``/*! … */``,
    #    que sqlglot adjunta como comentario sin tokenizar).
    for level, pattern, code, message in _TEXT_ELEVATORS:
        if _RANK[level] > _RANK[danger] and pattern.search(scanned):
            danger = level
            if not any(r.code == code for r in reasons):
                reasons.append(Reason(code, message))

    # 6) Escritura sobre esquemas del sistema: leerlos sí, modificarlos no.
    #    Se escanea también la variante SIN comillas: ``_scan_normalize`` las conserva a
    #    propósito, y el patrón exige ``esquema.`` — con backticks (``` `mysql`.`user` ```)
    #    el cierre rompía el match y la escritura pasaba como un simple ``write``.
    if danger != READ and (
        _SYSTEM_SCHEMA_RE.search(scanned) or _SYSTEM_SCHEMA_RE.search(_unquoted(scanned))
    ):
        reasons.append(
            Reason(
                "system_schema_write",
                "La sentencia modifica un esquema del sistema del motor. Leerlos está "
                "permitido; escribirlos corrompería el propio servidor.",
            )
        )
        danger = BLOCKED
        kind = "blocked"

    return StatementPlan(
        seq=seq,
        sql=sql,
        kind=kind,
        danger=danger,
        reasons=tuple(reasons),
        impact_query=_impact_query(tree, dialect) if danger == WRITE else None,
        fetch_sql=_limited_sql(tree, dialect, max_rows) if danger != BLOCKED else None,
    )


def classify(sql: str, *, engine: str, max_rows: int | None = None) -> QueryPlan:
    """
    Clasifica un lote SQL completo. El peligro del lote es el **máximo** de sus
    sentencias: una confirmación cubre el lote entero, nunca sentencia por sentencia.
    """
    statements = [s for s in split_sql_statements(sql) if s.strip()]
    plans = tuple(
        classify_statement(stmt, engine=engine, seq=i, max_rows=max_rows)
        for i, stmt in enumerate(statements)
    )
    danger = worst(*(p.danger for p in plans)) if plans else READ

    reasons: list[Reason] = []
    seen: set[str] = set()

    # Defensa en profundidad: la blocklist se corre TAMBIÉN sobre el lote crudo. El
    # splitter descarta las "sentencias" que son solo comentarios, así que un
    # ``/*!40101 GRANT … */`` (comentario EJECUTABLE de MySQL) no llegaría a
    # ``classify_statement``. Hoy tampoco llegaría al motor —el runner ejecuta lo que el
    # splitter devuelve—, pero esa protección es un efecto colateral del splitter, no una
    # decisión de seguridad: si su criterio cambia, este escaneo sigue bloqueando.
    for reason in _blocklist_hits(_scan_normalize(sql, engine=engine)):
        danger = BLOCKED
        if reason.code not in seen:
            seen.add(reason.code)
            reasons.append(reason)

    for p in plans:
        for r in p.reasons:
            if r.code not in seen:
                seen.add(r.code)
                reasons.append(r)
    return QueryPlan(
        statements=plans,
        danger=danger,
        sql_hash=sql_hash(sql),
        reasons=tuple(reasons),
    )


# --------------------------------------------------------------------------- #
# Utilidades para el controller                                                #
# --------------------------------------------------------------------------- #


def sql_hash(sql: str) -> str:
    """
    Huella del SQL para ATAR el ``confirm_token`` al texto exacto que se previsualizó.

    Sin esto se podría pedir el preview de un ``SELECT`` y ejecutar un ``DROP`` con el
    mismo token. Se normaliza solo el espaciado de los extremos: cualquier otro cambio
    invalida el token y obliga a repetir el preview (que es justo lo que se busca).
    """
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


_SECRET_RE = re.compile(
    r"(IDENTIFIED\s+(?:WITH\s+\S+\s+)?(?:BY|AS)\s+|(?:ENCRYPTED\s+)?PASSWORD\s+)"
    r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")",
    re.IGNORECASE,
)


def redact_secrets(sql: str) -> str:
    """
    Enmascara literales de contraseña antes de PERSISTIR el SQL (historial/auditoría).

    Sin esto, un ``ALTER USER … IDENTIFIED BY 'x'`` dejaría la contraseña en claro en la
    base del gateway. Corre aunque esas sentencias estén en la blocklist: un intento
    BLOQUEADO también se audita, y el texto que se guarda es el que el admin envió.
    """
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}'***'", sql)


def is_gateway_metadata_target(
    *,
    host: str,
    port: int,
    database: str,
    gateway_host: str,
    gateway_port: int | str,
    gateway_database: str,
) -> bool:
    """
    ¿El destino elegido es la PROPIA base de metadatos del gateway?

    Footgun real: si la BD del gateway vive en un servidor del inventario, la consola
    permitiría dropear sus propias tablas — el inventario, la auditoría, el historial y
    ``server_users``, que guarda las credenciales pseudo-root CIFRADAS de todos los
    servidores gestionados. Pérdida del plano de control junto con su propia auditoría.

    Se compara resolviendo AMBOS hosts a IPs, no por texto: comparar strings dejaba pasar
    cualquier grafía equivalente del mismo destino. El caso realista no es exótico — con
    la base del gateway en el host ``db`` de un compose, basta registrar un servidor
    apuntando a ``172.18.0.2`` (la misma máquina) para evadir el guard, y esa IP privada
    además pasa el filtro anti-SSRF por diseño.

    **Fail-closed**: si la resolución falla, se cae a la comparación textual en vez de
    devolver "no es el gateway".
    """
    try:
        same_port = int(port) == int(gateway_port)
    except (TypeError, ValueError):
        same_port = False
    # El nombre de la BD se compara exacto: en MySQL sobre Linux es sensible a mayúsculas.
    if not same_port or (database or "").strip() != (gateway_database or "").strip():
        return False

    same_host = (host or "").strip().lower() == (gateway_host or "").strip().lower()
    if same_host:
        return True
    try:
        from app.core.net_guard import _resolve_ips

        return bool(set(_resolve_ips(host)) & set(_resolve_ips(gateway_host)))
    except Exception:  # noqa: BLE001 — sin resolución queda la comparación textual
        return same_host
