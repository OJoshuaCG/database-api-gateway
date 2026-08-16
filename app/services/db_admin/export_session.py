"""
La transacción de consistencia de una exportación (§6 del diseño del módulo 10).

Una exportación tiene que describir **un solo instante** del origen (§19.4). Eso exige una
**única conexión** dedicada al job, con una transacción de lectura abierta antes del primer
objeto y cerrada después del último — no una conexión por tabla, que es lo que hacía hasta
ahora cada método de introspección del adapter (de ahí la inyección de conexión del §6.4).

**El límite irreducible, que se reporta en vez de taparse (§6.2):**

===============  ==================================  ==========  =====
Motor            Transacción                         Estructura  Datos
===============  ==================================  ==========  =====
PostgreSQL       REPEATABLE READ + READ ONLY         sí          sí
MySQL / MariaDB  REPEATABLE READ + CONSISTENT
                 SNAPSHOT + READ ONLY                **no**      sí (solo InnoDB)
===============  ==================================  ==========  =====

En la familia MySQL el snapshot consistente de InnoDB es MVCC de **filas**: el diccionario
de datos y ``information_schema`` no participan, así que un ``ALTER TABLE`` concurrente se ve
de inmediato. Congelar también el catálogo exigiría ``FLUSH TABLES WITH READ LOCK``, que pide
privilegio ``RELOAD`` y **bloquea las escrituras del servidor entero** — inaceptable en un
gateway que administra bases de terceros. Es exactamente la limitación de
``mysqldump --single-transaction``. Por eso ``supports_consistent_structure`` es una
propiedad del MOTOR real y no una constante: el preview y el manifiesto emiten el aviso a
partir de ella.

**Los costos que hay que asumir (§6.3).** Una transacción de lectura larga retiene versiones
viejas: en PostgreSQL bloquea el ``VACUUM`` y hace crecer las tablas, y en la familia MySQL
infla el historial de undo. Un export de horas **degrada el origen**. De ahí las dos defensas
de este módulo:

- ``EXPORT_MAX_DURATION_SECONDS``: aborto duro con **cierre garantizado en un ``finally``**.
  Una transacción huérfana contra la base de un cliente es un incidente de producción.
- ``EXPORT_IDLE_TRANSACTION_TIMEOUT_MS``: ``remote_engine`` ata
  ``idle_in_transaction_session_timeout`` al ``statement_timeout`` dentro de los
  ``connect_args`` del engine, así que un export estancado sostendría el snapshot de PG
  durante todo el timeout de sentencia. Se desacopla con un ``SET`` **a nivel de sesión**
  sobre esta conexión —que gana sobre el ``-c`` de la URL— en vez de tocar ``remote_engine``:
  cambiar los ``connect_args`` habría metido otro eje en la clave del cache de engines y
  habría afectado a los otros cinco consumidores para resolver un problema de uno solo.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.environments import (
    EXPORT_BATCH_ROWS,
    EXPORT_IDLE_TRANSACTION_TIMEOUT_MS,
    EXPORT_MAX_DURATION_SECONDS,
    EXPORT_STATEMENT_TIMEOUT_MS,
)
from app.core.logger import get_logger
from app.core.remote_engine import ServerTarget, database_connection, map_driver_error

logger = get_logger(__name__)

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})

# Motores donde la transacción de lectura cubre TAMBIÉN el catálogo. Es una lista blanca
# (fail-closed): un motor nuevo no hereda una garantía que nadie verificó.
_CONSISTENT_STRUCTURE_ENGINES = frozenset({"postgresql"})


class ExportDurationExceeded(Exception):
    """
    La corrida superó ``EXPORT_MAX_DURATION_SECONDS``.

    No es una ``AppHttpException``: quien la ve es el worker de F4, no una request. El
    contrato es que el ``finally`` del context manager ya cerró la transacción cuando esta
    excepción llega al llamador — lo único que le queda por hacer es marcar el job.
    """


@dataclass
class ExportSession:
    """
    La conexión y la transacción de UNA corrida de exportación.

    No se instancia a mano: la produce el context manager ``export_session``, que es el que
    garantiza el cierre.
    """

    conn: Connection
    engine: str
    database: str
    deadline_monotonic: float | None
    # ¿La transacción abierta cubre el catálogo, o solo los datos? Lo decide el MOTOR REAL
    # (ver ``_CONSISTENT_STRUCTURE_ENGINES``), no una constante del módulo: el aviso del
    # §6.2 tiene que salir de la misma fuente que la garantía.
    consistent_structure: bool = False
    # Directivas de sesión que el motor rechazó (se degrada y se REPORTA, nunca en silencio).
    degradations: list[str] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def supports_consistent_structure(self) -> bool:
        """¿El artefacto puede afirmar que estructura y datos son del mismo instante?"""
        return self.consistent_structure

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def remaining_seconds(self) -> float | None:
        """Segundos hasta el aborto duro; ``None`` = sin tope configurado."""
        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - time.monotonic()

    def check_deadline(self) -> None:
        """
        Aborto duro por duración (§6.3). Se comprueba de forma COOPERATIVA —entre objetos y
        entre lotes de filas— y no con un vigilante en otro hilo: cancelar una sentencia
        desde afuera exige matar la sesión en el motor, y para eso ya está el
        ``statement_timeout`` de la conexión, que acota lo único que un chequeo cooperativo
        no puede interrumpir (una consulta que no vuelve).
        """
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise ExportDurationExceeded(
                f"la exportación superó el máximo de {EXPORT_MAX_DURATION_SECONDS} s"
            )

    # ------------------------------------------------------------------ #
    # Lectura                                                             #
    # ------------------------------------------------------------------ #
    def iter_rows(
        self, select_sql: str, *, batch_rows: int = EXPORT_BATCH_ROWS
    ) -> Iterator[Sequence[Any]]:
        """
        Rinde las filas de una consulta en STREAMING, dentro de la transacción del job.

        Mismo mecanismo que ``data_copy._iter_source_rows`` (``stream_results`` +
        ``yield_per``), que ya está probado contra los tres motores: el consumo de memoria
        del gateway es plano e independiente del tamaño de la tabla, que es criterio de
        aceptación del §8.1.

        La diferencia con ``data_copy`` es la conexión: allá se abre una propia por copia,
        acá se usa la del job — si no, las filas vendrían de otro instante que el catálogo.

        Las opciones van **por SENTENCIA** (``text(...).execution_options(...)``) y no por
        conexión. ``Connection.execution_options()`` muta la conexión IN-PLACE (a diferencia
        de ``Engine.execution_options()``, que devuelve una copia), y esta conexión vive el
        job entero: fijarle ``stream_results`` acá se lo dejaba pegado al re-snapshot final
        de drift y a ``counter_value``, que pasaban a ejecutarse por un cursor con nombre.
        Es exactamente el modo de fallo que documenta ``query_runner`` — en psycopg un
        cursor con nombre se compone como ``DECLARE … CURSOR FOR <sentencia>``, gramática
        que solo acepta consultas.
        """
        self.check_deadline()
        result = self.conn.execute(
            text(select_sql).execution_options(
                stream_results=True, yield_per=max(1, batch_rows)
            )
        )
        try:
            for row in result:
                yield row
        finally:
            result.close()

    def scalar(self, sql: str, params: dict | None = None):
        """Un valor suelto del catálogo (contadores de autoincremento) en la misma sesión."""
        self.check_deadline()
        return self.conn.execute(text(sql), params or {}).scalar()


@contextmanager
def export_session(
    target: ServerTarget,
    database: str,
    *,
    engine: str,
    max_duration_seconds: int = EXPORT_MAX_DURATION_SECONDS,
    statement_timeout_ms: int = EXPORT_STATEMENT_TIMEOUT_MS,
    idle_timeout_ms: int = EXPORT_IDLE_TRANSACTION_TIMEOUT_MS,
) -> Iterator[ExportSession]:
    """
    Abre la conexión dedicada del job y su transacción de lectura; la cierra SIEMPRE.

    ``bulk=True`` + ``statement_timeout_ms`` explícito: el timeout interactivo (15 s)
    cancelaría el ``SELECT`` de una tabla grande a mitad y el bulk de una hora es el techo de
    la copia del clon, no el de una lectura que además sostiene un snapshot.

    El cierre va en un ``finally`` por el motivo del §6.3: una transacción huérfana contra la
    base de un tercero bloquea su ``VACUUM`` (PG) o infla su undo (MySQL) hasta que alguien
    la mata a mano. El ``rollback`` explícito antes del ``close`` es deliberado: la
    exportación no escribe nada, así que revertir es semánticamente correcto y además libera
    el snapshot ANTES de devolver la conexión, sin depender de que el driver lo haga.
    """
    deadline = (
        time.monotonic() + max_duration_seconds if max_duration_seconds > 0 else None
    )
    with database_connection(
        target, database, bulk=True, statement_timeout_ms=statement_timeout_ms
    ) as conn:
        session = ExportSession(
            conn=conn,
            engine=engine,
            database=database,
            deadline_monotonic=deadline,
            consistent_structure=engine in _CONSISTENT_STRUCTURE_ENGINES,
        )
        try:
            _begin_read_transaction(session, idle_timeout_ms=idle_timeout_ms)
            yield session
        finally:
            # Nada de lo que pase acá puede tapar la excepción original del cuerpo: si el
            # rollback falla (la sesión ya la mató el motor por el idle timeout, típicamente)
            # se registra y se sigue — la conexión se cierra igual al salir del ``with``.
            try:
                conn.rollback()
            except SQLAlchemyError:
                logger.warning(
                    "export_session: el rollback de cierre falló (la sesión pudo haber "
                    "sido terminada por el motor); la conexión se cierra igual"
                )


def _begin_read_transaction(session: ExportSession, *, idle_timeout_ms: int) -> None:
    """Abre la transacción de lectura según la tabla del §6.1."""
    conn = session.conn
    engine = session.engine
    try:
        if engine == "postgresql":
            # Vía SQLAlchemy y no un ``BEGIN`` crudo: psycopg abre la transacción por su
            # cuenta al primer ``execute``, así que un ``BEGIN`` nuestro llegaría SEGUNDO y
            # el servidor respondería "there is already a transaction in progress" dejando
            # la sesión en el aislamiento por defecto (READ COMMITTED) — es decir, sin
            # snapshot y sin que nada fallara. ``execution_options`` lo fija en la conexión
            # DBAPI antes de que empiece.
            conn.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            _set_pg_idle_timeout(session, idle_timeout_ms)
            # Fuerza el arranque de la transacción AHORA: el snapshot de REPEATABLE READ se
            # toma en la primera sentencia, no en el BEGIN, y queremos que sea antes de leer
            # el catálogo y no en algún punto intermedio del job.
            conn.exec_driver_sql("SELECT 1")
        elif engine in _MYSQL_FAMILY:
            # Dos pasos en vez del ``START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY``
            # de una sola línea que propone el §6.1: la lista de características separadas
            # por coma no está soportada de forma uniforme en todas las versiones de MySQL
            # 5.7/8 y MariaDB 10.x/11.x, mientras que fijar el modo de acceso por SESIÓN sí
            # lo está desde MySQL 5.6 / MariaDB 10.0. La garantía resultante es idéntica —el
            # START TRANSACTION sin modo explícito hereda el de la sesión— y la conexión es
            # dedicada al job con ``NullPool``, así que dejarla en READ ONLY no contamina a
            # nadie: se descarta al cerrar.
            conn.exec_driver_sql(
                "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            )
            conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            # ``WITH CONSISTENT SNAPSHOT`` toma el read-view de InnoDB en este preciso
            # instante, no en la primera lectura. Solo cubre InnoDB: una tabla MyISAM del
            # origen se lee sin ninguna garantía de instante (límite del motor).
            conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        else:
            # Motor no cubierto (SQLite en las pruebas locales): no se abre ninguna
            # transacción especial y NO se afirma consistencia. Fail-closed.
            session.consistent_structure = False
            session.degradations.append(
                f"El motor '{engine}' no tiene transacción de lectura consistente "
                "configurada: el artefacto no puede afirmar un punto único en el tiempo."
            )
    except SQLAlchemyError as exc:
        raise map_driver_error(
            exc,
            op="export_session.begin",
            target=None,
            extra={"database": session.database, "engine": engine},
        )


def _set_pg_idle_timeout(session: ExportSession, idle_timeout_ms: int) -> None:
    """
    Desacopla ``idle_in_transaction_session_timeout`` del ``statement_timeout`` (§6.3).

    Se hace con un ``SET`` de SESIÓN sobre esta conexión, que tiene precedencia sobre el
    ``-c`` que ``remote_engine`` puso en la URL. La alternativa —agregar el parámetro a
    ``_connect_args``— habría metido otro eje en la clave del cache de engines y habría
    tocado a los cinco consumidores de ``remote_engine`` para resolver el problema de uno.

    Best-effort explícito: si el motor rechaza el ``SET`` (un PostgreSQL gestionado que lo
    tenga bloqueado), se anota en ``degradations`` y se sigue. No es una garantía de
    corrección sino una red de seguridad operativa; abortar el export por no poder ajustarla
    sería peor que correr con el timeout heredado.
    """
    if idle_timeout_ms < 0:
        return
    try:
        session.conn.exec_driver_sql(
            f"SET idle_in_transaction_session_timeout = {int(idle_timeout_ms)}"
        )
    except SQLAlchemyError:
        session.degradations.append(
            "No se pudo ajustar idle_in_transaction_session_timeout: si la exportación se "
            "estanca, el snapshot se sostiene hasta el timeout de sentencia."
        )


__all__ = [
    "ExportDurationExceeded",
    "ExportSession",
    "export_session",
]
