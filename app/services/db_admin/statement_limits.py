"""
Timeouts de SESIÓN de una conexión a un motor de usuario, compartidos por la consola SQL y por las
lecturas de datos del agente.

Se extrajo de ``query_runner`` para que el servicio del agente (``agent_query``) no importe la
consola entera y para que haya UN solo criterio de qué variable de sesión se fija en cada motor. La
consola sigue llamándola desde ``_prepare_session`` sin ningún cambio de comportamiento.
"""

from sqlalchemy.exc import SQLAlchemyError

from app.core.logger import get_logger

logger = get_logger(__name__)


def apply_session_timeouts(conn, engine: str, timeout_ms: int) -> None:
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


def apply_postgres_statement_timeout(conn, timeout_ms: int) -> None:
    """
    ``SET statement_timeout`` explícito en PostgreSQL. La conexión ya lo lleva en sus opciones
    (``-c statement_timeout``), pero el engine se cachea con el timeout CUANTIZADO en tramos de 5 s
    (``remote_engine._quantize_timeout``): un timeout de 20 s y uno de 18 s comparten engine. Esto fija
    el valor exacto de ESTA ejecución. Solo para el hook de las lecturas de datos: la consola no lo
    llama, así que su comportamiento no cambia.
    """
    if timeout_ms <= 0:
        return
    conn.exec_driver_sql(f"SET statement_timeout = {int(timeout_ms)}")


__all__ = ["apply_postgres_statement_timeout", "apply_session_timeouts"]
