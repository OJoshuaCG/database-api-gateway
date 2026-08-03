"""
Ejecución de la CONSOLA SQL contra un servidor destino.

Complementa a ``query_policy`` (que decide QUÉ se puede ejecutar) haciéndose cargo de
CÓMO se ejecuta: con qué credencial, dentro de qué transacción, con qué topes y cómo se
reportan los errores del motor.

CUATRO DECISIONES
-----------------

**1. El nivel ``read`` lo hace cumplir el MOTOR, no el parser.** Toda ejecución de
lectura corre dentro de una transacción de SOLO LECTURA (``START TRANSACTION READ ONLY``
en MySQL/MariaDB, ``SET TRANSACTION READ ONLY`` en PostgreSQL). Si la clasificación
estática se equivocara —un ``SELECT fn()`` cuya función escribe—, el motor aborta la
sentencia. Sin esto, "solo lectura" sería una promesa del parser; con esto es una
garantía del servidor.

**2. Un error del motor NO es un error de la API.** El propósito de la consola es probar
permisos, así que un ``SELECT command denied to user 'x'@'y'`` es una prueba EXITOSA y se
devuelve como resultado estructurado (HTTP 200, ``success=false``), no como un 403. Por
eso este módulo NO usa ``map_driver_error`` para los errores de ejecución: ese traductor
convierte 1142/42501 en un 403 genérico ("la credencial del gateway no tiene permisos")
que oculta justo el mensaje que se quiere leer. ``map_driver_error`` se sigue usando para
los fallos de INFRAESTRUCTURA (host inalcanzable, timeout de conexión), que sí son
errores de la API.

**3. La credencial elegida entra en la clave del cache de engines.** Ver
``remote_engine._engines``: sin el usuario en la clave, una prueba "como usuario
limitado" reusaría el engine pseudo-root y daría verde siempre.

**4. El tope de filas se empuja al MOTOR, no se aplica del lado del gateway.** Recortar
con ``fetchmany`` acota la memoria del proceso pero NO el transporte: en MySQL/MariaDB,
cerrar un cursor sin agotarlo dispara ``MySQLResult._finish_unbuffered_query()``, que
—según el comentario del propio pymysql— gira leyendo paquetes hasta el EOF *porque no hay
forma de que el servidor deje de mandarlos*. Un ``SELECT * FROM tabla_de_50M`` con tope de
1000 filas transfería igual las 50M. Por eso ``query_policy`` emite un ``fetch_sql`` con el
``LIMIT`` incorporado (una fila de más, para informar ``truncated`` con certeza) y este
módulo ejecuta ESE SQL. Para lo que no se puede acotar sin cambiar la semántica
(``SHOW``, ``FOR UPDATE``, sentencias opacas) queda el recorte del lado del gateway, que
sigue protegiendo la memoria.

**NO se usa ``stream_results``.** Fijarlo a nivel de conexión hacía que SQLAlchemy enrutara
TODA sentencia por un cursor con nombre, y en psycopg un cursor con nombre se compone como
``DECLARE … CURSOR FOR <sentencia>`` — gramática que solo acepta consultas. Con eso,
*toda* ejecución contra PostgreSQL moría en la primera sentencia de ``_prepare_session``
(``SET TRANSACTION READ ONLY``) con un error de sintaxis que además se reportaba como
"no se pudo conectar". El ``LIMIT`` empujado al motor resuelve el mismo problema sin
cursores server-side.
"""

import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as time_cls, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.logger import get_logger
from app.core.remote_engine import ServerTarget, database_connection, map_driver_error
from app.services.db_admin.identifiers import quote_identifier
from app.services.db_admin.query_policy import DDL, StatementPlan

logger = get_logger(__name__)

# Modos de conexión soportados.
MODE_ADMIN = "admin"
MODE_STORED = "stored"
MODE_PROVIDED = "provided"
MODE_IMPERSONATE = "impersonate"


@dataclass(frozen=True)
class QueryCredential:
    """
    Cómo conectarse para ESTA ejecución. ``password`` llega YA descifrado y solo vive en
    memoria el tiempo de abrir la conexión; nunca se persiste ni se loguea.

    ``impersonate`` es exclusivo de PostgreSQL: se conecta con la credencial pseudo-root
    y se emite ``SET ROLE``, que en PostgreSQL permite a un superusuario adoptar cualquier
    rol SIN conocer su contraseña — la única forma de probar permisos de un rol cuya
    contraseña el gateway nunca fijó. MySQL/MariaDB no tienen equivalente (su ``SET ROLE``
    solo alcanza roles ya otorgados al usuario actual), así que allí hace falta credencial
    real.
    """

    mode: str
    username: str
    password: str | None = None
    impersonate_role: str | None = None


@dataclass(frozen=True)
class ExecError:
    """Error DEL MOTOR (no de la API). ``code`` es el errno/SQLSTATE nativo."""

    code: str | None
    sqlstate: str | None
    message: str


@dataclass
class StatementOutcome:
    seq: int
    sql: str
    kind: str
    danger: str
    success: bool
    duration_ms: int
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    rows_affected: int | None = None
    truncated: bool = False
    error: ExecError | None = None
    executed: bool = True
    # True => el motor rechazó la sentencia por la transacción de SOLO LECTURA, es decir,
    # la política la clasificó como lectura y no lo era.
    policy_miss: bool = False


@dataclass
class ExecutionOutcome:
    statements: list[StatementOutcome]
    success: bool
    committed: bool
    rolled_back: bool
    connection_error: ExecError | None = None
    warnings: list[str] = field(default_factory=list)
    # True => quedaron cambios de esquema aplicados en disco pese al ROLLBACK (commit
    # implícito del DDL en MySQL/MariaDB).
    ddl_persisted: bool = False


# Errores que son un RESULTADO de la prueba, no un fallo de infraestructura. Se devuelven
# como resultado estructurado (HTTP 200) en vez de como 5xx.
#
# Están separados en dos grupos porque el mismo código significa cosas distintas según
# CON QUÉ credencial se conectó: un 1045 probando el usuario `app_ro` es el resultado
# buscado, pero un 1045 en modo `admin`/`impersonate` —que usan la credencial pseudo-root
# del gateway— es un problema de CONFIGURACIÓN del gateway y debe salir como error.
_AUTH_LIKE_ALWAYS: frozenset[Any] = frozenset(
    {
        1044,  # access denied for user to database
        1049,  # unknown database
        1698,  # access denied (auth_socket)
        "3D000",  # invalid_catalog_name
        "42704",  # undefined_object: rol inexistente (PG < 16 en algunos caminos)
        "42501",  # insufficient_privilege
        # PostgreSQL devuelve 22023 (invalid_parameter_value), NO 42704, cuando el rol de
        # un SET ROLE no existe: check_role() usa GUC_check_errmsg sin fijar errcode, así
        # que hereda el default de guc.c. Sin esto, un rol mal tipeado en modo impersonate
        # salía como HTTP 500.
        "22023",
    }
)
_AUTH_LIKE_TESTED_CREDENTIAL: frozenset[Any] = frozenset(
    {
        1045,  # access denied for user (contraseña incorrecta)
        1130,  # host not privileged: el análogo MySQL del 28000 de PG, y probar
        #        restricciones por host es EL caso de uso en MySQL/MariaDB
        1226,  # max_user_connections del usuario probado
        1203,  # max_connections_per_hour del usuario probado
        1275,  # server is in secure auth mode
        3159,  # secure transport required (configuración de la cuenta)
        "28P01",  # invalid_password
        "28000",  # invalid_authorization_specification (sin entrada en pg_hba.conf)
    }
)

# Sentencia que fuerza la transacción de SOLO LECTURA, por familia de motor.
# MySQL/MariaDB: ``START TRANSACTION READ ONLY`` debe ser la PRIMERA sentencia (su
# ``SET TRANSACTION`` no está permitido con una transacción ya activa).
# PostgreSQL: ``SET TRANSACTION READ ONLY`` solo vale antes de la primera consulta de la
# transacción, que es exactamente donde se emite.
_READ_ONLY_SQL = {
    "mysql": "START TRANSACTION READ ONLY",
    "mariadb": "START TRANSACTION READ ONLY",
    "postgresql": "SET TRANSACTION READ ONLY",
}


def _escape_percent(stmt: str) -> str:
    """
    ``%`` literal -> ``%%``. ``exec_driver_sql`` llega al DBAPI con params distilados a
    ``()`` (nunca ``None``), así que pymysql/psycopg parsean placeholders ``%s`` y un
    ``%`` literal (``LIKE '%x%'``, ``DATE_FORMAT(…, '%Y')``) reventaría antes de llegar
    al motor. Mismo criterio que ``MigrationRunner._escape_percent``.
    """
    return stmt.replace("%", "%%")


def _error_info(exc: Exception) -> ExecError:
    """
    Traduce una excepción del driver a ``ExecError`` sin filtrar la URL de conexión ni la
    credencial: se usa el mensaje de ``exc.orig`` (el del motor), no el de SQLAlchemy, que
    adjunta el SQL completo y detalles de la conexión.
    """
    orig = getattr(exc, "orig", None) or exc
    sqlstate = getattr(orig, "sqlstate", None)
    code: Any = None
    args = getattr(orig, "args", None)
    if args:
        first = args[0]
        if isinstance(first, int):
            code = first
        elif isinstance(first, str) and len(first) <= 5 and first.isalnum():
            code = first
    if code is None and sqlstate:
        code = sqlstate
    return ExecError(
        code=str(code) if code is not None else None,
        sqlstate=str(sqlstate) if sqlstate else None,
        message=str(orig)[:1000],
    )


def _is_auth_like(err: ExecError, *, mode: str = MODE_PROVIDED) -> bool:
    """
    ¿El error es un RESULTADO de la prueba de permisos, o un fallo del gateway?

    En ``admin``/``impersonate`` la conexión usa la credencial pseudo-root del gateway, así
    que un "access denied" ahí NO es el resultado de nada: es el gateway mal configurado y
    debe salir como error de la API, no como un 200 con ``success=false``.
    """
    codes = set(_AUTH_LIKE_ALWAYS)
    if mode not in (MODE_ADMIN, MODE_IMPERSONATE):
        codes |= _AUTH_LIKE_TESTED_CREDENTIAL
    if err.sqlstate and err.sqlstate in codes:
        return True
    if err.code is None:
        return False
    if err.code in codes:
        return True
    try:
        return int(err.code) in codes
    except (TypeError, ValueError):
        return False


# Un fallo de la transacción de SOLO LECTURA significa que la CLASIFICACIÓN se equivocó:
# el motor atajó una escritura que la política dejó pasar como lectura. Los dos motores
# coinciden en SQLSTATE 25006 (MySQL/MariaDB: errno 1792). Merece señal propia — es el
# bucle de realimentación para mejorar la política con datos de producción.
def _is_policy_miss(err: ExecError) -> bool:
    return err.sqlstate == "25006" or err.code in ("1792", "25006")


def _driver_failure(exc: Exception, seq: int) -> ExecError:
    """
    Error del driver que NO es del motor (decodificación, memoria, protocolo).

    No se propaga ``str(exc)``: un ``UnicodeDecodeError`` lleva los bytes crudos de una
    fila del cliente, y esos datos no deben salir por la API ni entrar en el historial.
    """
    logger.warning("Fallo no-DBAPI procesando la sentencia seq=%s", seq, exc_info=True)
    return ExecError(
        code=None,
        sqlstate=None,
        message=(
            f"El driver falló procesando el resultado ({type(exc).__name__}). "
            "Suele indicar datos con una codificación distinta a la de la conexión."
        ),
    )


# --------------------------------------------------------------------------- #
# Normalización de valores para JSON                                           #
# --------------------------------------------------------------------------- #


_MAX_CONTAINER_ITEMS = 200
_MAX_CONTAINER_DEPTH = 8


def _json_value(value: Any, max_chars: int, depth: int = 0) -> Any:
    """
    Convierte un valor del driver a algo serializable a JSON, recortando celdas grandes.

    ``Decimal`` se pasa a ``str`` a propósito (un float perdería precisión, que es justo
    lo que se está inspeccionando en una consola). Los binarios se muestran en hexadecimal
    con marca de recorte: una consola no debe volcar un BLOB entero por la API.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity no son JSON válido.
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        text = raw.hex()
        if len(text) > max_chars:
            return f"0x{text[:max_chars]}… ({len(raw)} bytes)"
        return f"0x{text}"
    if isinstance(value, (datetime, date, time_cls)):
        return value.isoformat()
    if isinstance(value, timedelta):
        # El tipo TIME de MySQL/MariaDB llega como timedelta y admite valores negativos y
        # mayores a 24 h. ``str()`` los rendea como ``-1 day, 23:00:00`` (para
        # ``TIME '-01:00:00'``) o ``34 days, 22:00:00`` (para ``TIME '838:00:00'``):
        # formalmente correcto e inservible en una consola. Se rearma como HH:MM:SS.
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    # ``max_chars`` acota cada HOJA, no el total: un JSON/JSONB con miles de cadenas
    # cortas se serializaría entero. ``_MAX_CONTAINER_ITEMS`` pone el techo de cardinalidad
    # y ``depth`` corta la recursión de un documento profundo.
    if isinstance(value, (list, tuple)):
        if depth >= _MAX_CONTAINER_DEPTH:
            return f"[…{len(value)} elementos]"
        head = [_json_value(v, max_chars, depth + 1) for v in value[:_MAX_CONTAINER_ITEMS]]
        if len(value) > _MAX_CONTAINER_ITEMS:
            head.append(f"… ({len(value)} elementos en total)")
        return head
    if isinstance(value, dict):
        if depth >= _MAX_CONTAINER_DEPTH:
            return f"{{…{len(value)} claves}}"
        items = list(value.items())[:_MAX_CONTAINER_ITEMS]
        out = {str(k): _json_value(v, max_chars, depth + 1) for k, v in items}
        if len(value) > _MAX_CONTAINER_ITEMS:
            out["…"] = f"({len(value)} claves en total)"
        return out
    if isinstance(value, str):
        if len(value) > max_chars:
            return f"{value[:max_chars]}… (truncado, {len(value)} caracteres)"
        return value
    text = str(value)
    return text if len(text) <= max_chars else f"{text[:max_chars]}…"


# --------------------------------------------------------------------------- #
# Preparación de la sesión                                                     #
# --------------------------------------------------------------------------- #


def effective_target(target: ServerTarget, credential: QueryCredential) -> ServerTarget:
    """
    ``ServerTarget`` con la credencial de ESTA ejecución.

    ``admin``/``impersonate`` conectan con la credencial pseudo-root del servidor;
    ``stored``/``provided`` la reemplazan por la del usuario elegido.
    """
    if credential.mode in (MODE_ADMIN, MODE_IMPERSONATE):
        return target
    return replace(
        target,
        admin_user=credential.username,
        admin_password=credential.password or "",
    )


def _apply_statement_timeout(conn, engine: str, timeout_ms: int) -> None:
    """
    Timeout de sentencia a nivel de SESIÓN, además del que ya aplica la conexión.

    En PostgreSQL el ``statement_timeout`` ya viaja en los parámetros de conexión, así que
    esto solo aporta en MySQL/MariaDB, donde el límite de la conexión es un timeout de
    SOCKET: cancela matando la conexión, sin mensaje del motor. Con la variable de sesión
    el servidor cancela la consulta y devuelve un error legible. Best-effort: si el motor
    no la soporta, se sigue adelante con el timeout de socket.
    """
    if engine not in ("mysql", "mariadb") or timeout_ms <= 0:
        return
    seconds = max(1, int(round(timeout_ms / 1000)))
    # Se intentan las variables de AMBOS motores y se ignora la que no exista: un MariaDB
    # dado de alta como ``mysql`` (o al revés) es un error de inventario frecuente, y cada
    # SET sobrante es un no-op inocuo. Sin esto, un MariaDB mal clasificado se quedaba sin
    # ningún timeout de sentencia.
    for stmt in (
        # MySQL: milisegundos, y SOLO aplica a SELECT de solo lectura de nivel superior.
        f"SET SESSION max_execution_time = {int(timeout_ms)}",
        # MariaDB: segundos (double), y sí aborta cualquier consulta, no solo SELECT.
        f"SET SESSION max_statement_time = {timeout_ms / 1000.0}",
        # Sin estos dos, un UPDATE/ALTER que espera un lock NO tiene techo real: el
        # default de lock_wait_timeout (metadata locks) es de UN AÑO en MySQL y un día en
        # MariaDB. El timeout de socket corta al CLIENTE, pero el servidor sigue encolado
        # y termina aplicando la sentencia que la API ya reportó como vencida.
        f"SET SESSION lock_wait_timeout = {seconds}",
        f"SET SESSION innodb_lock_wait_timeout = {seconds}",
    ):
        try:
            conn.exec_driver_sql(stmt)
        except SQLAlchemyError:
            logger.debug("El motor no admite «%s»; se continúa.", stmt)


def _prepare_session(
    conn, *, engine: str, read_only: bool, credential: QueryCredential, timeout_ms: int
) -> None:
    """
    Deja la sesión lista ANTES de la primera sentencia del usuario. El ORDEN importa:

    1. Timeouts de sesión: van antes de abrir la transacción de solo lectura, porque
       ``START TRANSACTION`` cerraría la transacción implícita que abre un ``SET``.
    2. Transacción de SOLO LECTURA: debe ser la primera sentencia de la transacción en
       ambos motores.
    3. ``SET ROLE``: después del punto 2, porque ``SET TRANSACTION READ ONLY`` deja de
       estar permitido en cuanto la transacción ejecutó cualquier consulta. Un ``SET`` no
       es una escritura, así que es válido dentro de una transacción de solo lectura.
    """
    _apply_statement_timeout(conn, engine, timeout_ms)

    if read_only:
        conn.exec_driver_sql(_READ_ONLY_SQL[engine])

    if credential.mode == MODE_IMPERSONATE and credential.impersonate_role:
        conn.exec_driver_sql(
            f"SET ROLE {quote_identifier(credential.impersonate_role, 'postgresql')}"
        )


# --------------------------------------------------------------------------- #
# Ejecución                                                                    #
# --------------------------------------------------------------------------- #


def _run_one(
    conn, plan: StatementPlan, *, max_rows: int, max_cell_chars: int
) -> StatementOutcome:
    started = time.monotonic()
    # ``fetch_sql`` es la misma consulta con el LIMIT empujado al motor. Se informa cuál
    # se ejecutó realmente: mostrar el SQL original mientras se corre otro sería mentir.
    executed_sql = plan.fetch_sql or plan.sql

    def _fail(err: ExecError) -> StatementOutcome:
        return StatementOutcome(
            seq=plan.seq,
            sql=executed_sql,
            kind=plan.kind,
            danger=plan.danger,
            success=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=err,
            policy_miss=_is_policy_miss(err),
        )

    try:
        result = conn.exec_driver_sql(_escape_percent(executed_sql))
    except SQLAlchemyError as exc:
        return _fail(_error_info(exc))
    except Exception as exc:  # noqa: BLE001 — ver nota de abajo
        return _fail(_driver_failure(exc, plan.seq))

    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    rows_affected: int | None = None

    try:
        if result.returns_rows:
            columns = [str(c) for c in result.keys()]
            # Una fila DE MÁS: es la única forma de distinguir "hay exactamente max_rows"
            # de "hay más y se recortó". Cuando hay ``fetch_sql`` el motor ya mandó a lo
            # sumo ``max_rows + 1``, así que esto no transfiere nada extra.
            fetched = result.fetchmany(max_rows + 1)
            if len(fetched) > max_rows:
                truncated = True
                fetched = fetched[:max_rows]
            rows = [[_json_value(v, max_cell_chars) for v in row] for row in fetched]
        else:
            rows_affected = (
                result.rowcount
                if result.rowcount is not None and result.rowcount >= 0
                else None
            )
    except SQLAlchemyError as exc:
        return _fail(_error_info(exc))
    except Exception as exc:  # noqa: BLE001
        # El caso realista es ``UnicodeDecodeError``: pymysql decodifica con el charset de
        # la conexión y SIN ``errors=``, así que una columna con bytes inválidos (típico en
        # bases legacy) revienta durante el fetch. Y SQLAlchemy NO envuelve los errores del
        # camino de fetch (``handle_exception`` llega con ``statement=None``), así que sin
        # este except la excepción cruda subía como 500 y dejaba la auditoría con un
        # ``attempt`` huérfano — indistinguible de un intento que sí modificó datos.
        return _fail(_driver_failure(exc, plan.seq))
    finally:
        try:
            result.close()
        except Exception:  # noqa: BLE001 — cerrar el cursor nunca debe tapar el resultado
            logger.debug("No se pudo cerrar el cursor de seq=%s", plan.seq, exc_info=True)

    return StatementOutcome(
        seq=plan.seq,
        sql=executed_sql,
        kind=plan.kind,
        danger=plan.danger,
        success=True,
        duration_ms=int((time.monotonic() - started) * 1000),
        columns=columns,
        rows=rows,
        row_count=len(rows),
        rows_affected=rows_affected,
        truncated=truncated,
    )


def run_statements(
    target: ServerTarget,
    *,
    database: str,
    engine: str,
    statements: list[StatementPlan],
    credential: QueryCredential,
    read_only: bool,
    dry_run: bool = False,
    max_rows: int,
    max_cell_chars: int,
    timeout_ms: int,
) -> ExecutionOutcome:
    """
    Ejecuta el lote y devuelve un resultado por sentencia. Se detiene en el primer error
    (las restantes vuelven con ``executed=False``).

    Transaccionalidad: todo el lote va en UNA transacción y se commitea al final.
    ``read_only=True`` o ``dry_run=True`` terminan siempre en ROLLBACK. Esto da atomicidad
    real en PostgreSQL —incluido el DDL— y en el DML de MySQL/MariaDB; el DDL de
    MySQL/MariaDB hace COMMIT implícito y NO se puede deshacer, límite del motor que el
    llamador debe advertir.
    """
    eff_target = effective_target(target, credential)
    outcomes: list[StatementOutcome] = []
    warnings: list[str] = []

    # Un lote sin sentencias ejecutables (SQL que era solo comentarios) no justifica abrir
    # una conexión al destino ni figurar como "commiteado" en el historial.
    if not statements:
        return ExecutionOutcome(
            statements=[], success=True, committed=False, rolled_back=False
        )

    try:
        with database_connection(
            eff_target, database, statement_timeout_ms=timeout_ms
        ) as conn:
            _prepare_session(
                conn,
                engine=engine,
                read_only=read_only,
                credential=credential,
                timeout_ms=timeout_ms,
            )

            stopped = False
            for plan in statements:
                if stopped:
                    outcomes.append(
                        StatementOutcome(
                            seq=plan.seq,
                            sql=plan.sql,
                            kind=plan.kind,
                            danger=plan.danger,
                            success=False,
                            duration_ms=0,
                            executed=False,
                        )
                    )
                    continue
                outcome = _run_one(
                    conn, plan, max_rows=max_rows, max_cell_chars=max_cell_chars
                )
                outcomes.append(outcome)
                if not outcome.success:
                    stopped = True

            success = all(o.success for o in outcomes)
            must_rollback = read_only or dry_run or not success
            committed = not must_rollback

            # El DDL de MySQL/MariaDB commitea implícitamente, así que informar
            # ``rolled_back=True`` sobre un lote que ya creó/alteró tablas es MENTIRA — y
            # ese valor se persiste en el historial, que es lo que alguien va a mirar
            # durante un incidente. El aviso NO depende de ``dry_run``: un lote de dos
            # ALTER donde el segundo falla deja el primero aplicado en disco.
            ddl_persisted = engine in ("mysql", "mariadb") and any(
                o.danger == DDL and o.executed and o.success for o in outcomes
            )
            if must_rollback and ddl_persisted:
                warnings.append(
                    "MySQL/MariaDB hacen COMMIT implícito en cada sentencia DDL: las "
                    "sentencias de esquema que ya se ejecutaron quedaron aplicadas y el "
                    "ROLLBACK no las deshace."
                )

            # El cierre transaccional se aísla: si falla, el lote YA se ejecutó y el
            # resultado por sentencia es información crítica que no se puede descartar.
            try:
                if must_rollback:
                    conn.rollback()
                else:
                    conn.commit()
            except SQLAlchemyError:
                logger.error(
                    "Falló el cierre transaccional de la consola SQL", exc_info=True
                )
                committed = False
                success = False
                warnings.append(
                    "No se pudo cerrar la transacción: el estado final del lote es "
                    "INCIERTO y hay que verificarlo en el motor."
                )

            return ExecutionOutcome(
                statements=outcomes,
                success=success,
                committed=committed,
                rolled_back=must_rollback,
                warnings=warnings,
                ddl_persisted=ddl_persisted,
            )

    except SQLAlchemyError as exc:
        err = _error_info(exc)
        # Credencial inválida / rol inexistente / sin acceso a la BD: es el RESULTADO de
        # la prueba de permisos, no un fallo de infraestructura. Depende del MODO: con la
        # credencial pseudo-root un "access denied" es el gateway mal configurado.
        if _is_auth_like(err, mode=credential.mode):
            return ExecutionOutcome(
                statements=outcomes,
                success=False,
                committed=False,
                rolled_back=False,
                connection_error=err,
                warnings=warnings,
            )
        raise map_driver_error(
            exc,
            op="query_console.execute",
            target=eff_target,
            extra={"database": database},
        ) from exc


def estimate_impact(
    target: ServerTarget,
    *,
    database: str,
    engine: str,
    credential: QueryCredential,
    impact_queries: list[tuple[int, str]],
    timeout_ms: int,
) -> dict[int, int | None]:
    """
    Ejecuta los ``SELECT COUNT(*)`` derivados por ``query_policy`` y devuelve
    ``{seq: filas}``. Corre en una transacción de SOLO LECTURA y con la MISMA credencial
    que ejecutaría el UPDATE/DELETE: si el usuario no puede leer la tabla, el conteo
    queda en ``None`` en vez de mentir con el privilegio de otro.

    Un fallo del conteo NUNCA rompe el preview: la confirmación se sigue exigiendo, solo
    que sin cifra.
    """
    if not impact_queries:
        return {}

    results: dict[int, int | None] = {seq: None for seq, _ in impact_queries}
    eff_target = effective_target(target, credential)
    try:
        with database_connection(
            eff_target, database, statement_timeout_ms=timeout_ms
        ) as conn:
            _prepare_session(
                conn,
                engine=engine,
                read_only=True,
                credential=credential,
                timeout_ms=timeout_ms,
            )
            for seq, sql in impact_queries:
                try:
                    # SAVEPOINT por conteo: en PostgreSQL una sentencia fallida ABORTA la
                    # transacción, así que sin esto el primer COUNT que el usuario no
                    # puede leer dejaba los siguientes en 25P02 y el preview informaba
                    # "no se pudo contar" para TODAS — justo la cifra que se está mirando
                    # antes de confirmar algo destructivo.
                    with conn.begin_nested():
                        row = conn.exec_driver_sql(_escape_percent(sql)).fetchone()
                        results[seq] = (
                            int(row[0]) if row and row[0] is not None else None
                        )
                except Exception:  # noqa: BLE001 — estimar nunca rompe el preview
                    logger.debug(
                        "No se pudo estimar el impacto de seq=%s", seq, exc_info=True
                    )
            conn.rollback()
    except SQLAlchemyError:
        logger.debug("No se pudo abrir la conexión de estimación de impacto", exc_info=True)
    return results


__all__ = [
    "ExecError",
    "ExecutionOutcome",
    "MODE_ADMIN",
    "MODE_IMPERSONATE",
    "MODE_PROVIDED",
    "MODE_STORED",
    "QueryCredential",
    "StatementOutcome",
    "estimate_impact",
    "run_statements",
]
