"""
Espejo del historial de migraciones DENTRO de cada BD gestionada (``_datum_migrations``).

**Es un ESPEJO, no un libro mayor.** La autoridad sigue siendo
``database_migration_history`` en la BD del gateway. Esta tabla existe para una sola cosa que
aquella no puede dar: que la base se explique a sí misma. Restaurás un backup en otro lado,
le entregás la base al cliente, perdés el gateway — y el historial viaja con los datos.

Tres consecuencias de ser un espejo, y conviene tenerlas juntas:

1. **Se escribe fail-open.** Si la escritura falla, la migración SIGUE. Un espejo que puede
   tumbar un apply deja de ser un espejo y pasa a ser un punto de fallo nuevo en el camino
   más delicado del sistema.
2. **Por lo tanto puede tener huecos.** Nadie debe leerlo como fuente de verdad, ni acá ni
   desde el frontend. Un hueco significa "no se pudo escribir", jamás "no pasó".
3. **Misma ventana de doble escritura que el checkpoint**: el DDL de la migración y esta fila
   commitean por separado en MySQL/MariaDB (AUTOCOMMIT), así que un proceso que muere en el
   medio deja la migración aplicada y la fila sin escribir. Es el sentido seguro del error.

**El nombre es FIJO y el blueprint es una columna**, al revés que ``_gw_v_{slug}``. Esa
decisión es la que vuelve a esta tabla inmune a un renombrado de slug —lo que originó todo
este trabajo— y la que la deja lista para varios blueprints por base sin cambiarle nada.

Está en ``EXPORTABLE_INTERNAL_PREFIXES``: invisible al diff y al clon, pero incluida en el
export. Ver ``identifiers`` para el porqué de esa distinción.
"""

from datetime import datetime, timezone

from sqlalchemy import Connection, text

from app.core.logger import get_logger
from app.models.enums import EngineType
from app.services.db_admin.identifiers import quote_identifier

logger = get_logger(__name__)

#: Nombre FIJO, sin slug. Tiene que estar cubierto por ``GATEWAY_TABLE_PREFIXES`` y por
#: ``EXPORTABLE_INTERNAL_PREFIXES``, o el diff lo trataría como esquema del usuario.
MIRROR_TABLE = "_datum_migrations"

# El DDL se escribe por motor en vez de con un `Table` de SQLAlchemy a propósito: es una sola
# tabla, sin ORM detrás, y verla literal es lo que permite revisar de un vistazo que no lleve
# nada del negocio del cliente. El autoincremento es lo único que no es portable.
_DDL_MYSQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    model_id INT NOT NULL,
    blueprint_slug VARCHAR(120) NOT NULL,
    version VARCHAR(10) NOT NULL,
    direction VARCHAR(4) NOT NULL,
    status VARCHAR(10) NOT NULL,
    checksum CHAR(64) NULL,
    applied_at DATETIME(6) NOT NULL,
    execution_ms INT NULL,
    actor VARCHAR(128) NULL,
    request_id VARCHAR(32) NULL,
    PRIMARY KEY (id),
    KEY ix_datum_migrations_model_applied (model_id, applied_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS {table} (
    id BIGSERIAL PRIMARY KEY,
    model_id INTEGER NOT NULL,
    blueprint_slug VARCHAR(120) NOT NULL,
    version VARCHAR(10) NOT NULL,
    direction VARCHAR(4) NOT NULL,
    status VARCHAR(10) NOT NULL,
    checksum CHAR(64) NULL,
    applied_at TIMESTAMP NOT NULL,
    execution_ms INTEGER NULL,
    actor VARCHAR(128) NULL,
    request_id VARCHAR(32) NULL
)
"""

_INDEX_POSTGRES = (
    "CREATE INDEX IF NOT EXISTS ix_datum_migrations_model_applied "
    "ON {table} (model_id, applied_at)"
)

_INSERT = """
INSERT INTO {table}
    (model_id, blueprint_slug, version, direction, status, checksum,
     applied_at, execution_ms, actor, request_id)
VALUES (:model_id, :blueprint_slug, :version, :direction, :status, :checksum,
        :applied_at, :execution_ms, :actor, :request_id)
"""


def ensure_table(conn: Connection, engine: EngineType) -> None:
    """Crea la tabla si no está. Idempotente por el ``IF NOT EXISTS``.

    Se llama en cada escritura en vez de una sola vez al aprovisionar: el gateway adopta
    bases preexistentes y sobrevive a que alguien borre la tabla a mano, así que asumir que
    ya existe sería asumir un invariante que el sistema no garantiza. El costo es un DDL
    no-op por operación, no por sentencia.
    """
    tabla = quote_identifier(MIRROR_TABLE, engine.value)
    if engine == EngineType.postgresql:
        conn.exec_driver_sql(_DDL_POSTGRES.format(table=tabla))
        conn.exec_driver_sql(_INDEX_POSTGRES.format(table=tabla))
    else:
        conn.exec_driver_sql(_DDL_MYSQL.format(table=tabla))


def record(
    conn: Connection,
    engine: EngineType,
    *,
    model_id: int,
    blueprint_slug: str,
    entries: list[dict],
) -> int:
    """Escribe N filas en el espejo. **Fail-open**: devuelve cuántas logró escribir.

    NUNCA propaga. Un espejo que puede tumbar un apply deja de ser un espejo. El fallo se
    loguea con el detalle y la operación de migración sigue su curso — el historial
    autoritativo ya quedó en la BD del gateway.

    Cada ``entry`` lleva ``version``, ``direction``, ``status``, ``checksum``, ``applied_at``,
    ``execution_ms``, ``actor`` y ``request_id``.
    """
    if not entries:
        return 0
    try:
        ensure_table(conn, engine)
        # ``text()`` y NO ``exec_driver_sql``: los ``:nombre`` son binding de SQLAlchemy y el
        # driver crudo no los entiende. Acá corresponde al revés que en el runner de
        # migraciones —que usa ``exec_driver_sql`` justamente para que ``::`` de PostgreSQL y
        # los JSON en COMMENT no se lean como bind params—, porque este SQL es NUESTRO y lo
        # único variable son los valores, que van parametrizados.
        sql = text(_INSERT.format(table=quote_identifier(MIRROR_TABLE, engine.value)))
        for e in entries:
            conn.execute(
                sql,
                {
                    "model_id": model_id,
                    "blueprint_slug": blueprint_slug,
                    "version": e["version"],
                    "direction": e["direction"],
                    "status": e["status"],
                    "checksum": e.get("checksum"),
                    "applied_at": e.get("applied_at") or datetime.now(timezone.utc),
                    "execution_ms": e.get("execution_ms"),
                    "actor": e.get("actor"),
                    "request_id": e.get("request_id"),
                },
            )
        return len(entries)
    except Exception:  # noqa: BLE001 — fail-open deliberado, ver el docstring
        logger.exception(
            "espejo de migraciones: no se pudo escribir en %s (blueprint %s). La migración "
            "NO se ve afectada; el historial autoritativo está en la BD del gateway.",
            MIRROR_TABLE,
            blueprint_slug,
        )
        return 0
