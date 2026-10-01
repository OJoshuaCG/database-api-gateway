"""
Qué bases del motor NO puede usar ningún módulo como origen o destino, por lo que SON.

Es el par de guards que clon, export, consola y conversión de colación ya aplicaban cada uno
por su cuenta (``identifiers.ensure_not_reserved_database`` +
``query_policy.is_gateway_metadata_target``), en UNA función para que el próximo módulo no
pueda olvidar la mitad. Los módulos que lo omitían y lo usan ahora: snapshot / datos-semilla
(``ServerController``, y con eso ``POST /database-models/from-snapshot``) y comparación de
esquemas (``SchemaComparisonController``, los dos lados, en crear, preview, adoptar y ejecutar).
"""

from app.core.environments import DB_HOST, DB_NAME, DB_PORT
from app.exceptions import AppHttpException
from app.services.db_admin import query_policy
from app.services.db_admin.identifiers import reserved_database_names
from app.services.engine_database_catalog import CODE_SCOPE_NOT_ALLOWED

REASON_SYSTEM_DATABASE = "system_database"
REASON_GATEWAY_METADATA = "gateway_metadata"


def assert_database_in_scope(
    database: str, *, dialect: str, host: str, port: int, side: str
) -> None:
    """
    409 ``engine_database.scope_not_allowed`` si ``database`` es una base de sistema del motor
    o la base de metadatos del gateway.

    POR QUÉ, CON LOS DOS CASOS REALES QUE CERRÓ:

    - ``POST /database-models/from-snapshot {database: "mysql", data_tables: ["user"]}``
      extraía ``mysql.user`` —el ``authentication_string`` de TODAS las cuentas del motor—
      como INSERTs semilla de un blueprint, legible después por cualquiera con
      ``blueprints.read`` (``viewer``). El snapshot solo validaba el charset del nombre.
    - Una comparación de esquemas con TARGET = la base de metadatos co-alojada y un origen
      vacío renderizaba ``DROP TABLE`` de ``audit_log``/``users``/``server_users``: la
      destrucción que el guard de clon ya había cerrado para su propio módulo.
      ``_assert_live_exists`` no alcanzaba: ``list_databases`` filtra las bases de sistema,
      pero no la del gateway.

    ``side`` viaja en ``public_context`` para que la UI marque el campo culpable.
    """
    name = (database or "").strip()
    if name.lower() in reserved_database_names(dialect):
        raise AppHttpException(
            message=(
                f"'{name}' es una base de datos de sistema del motor: no se puede usar como "
                "origen ni como destino de esta operación."
            ),
            status_code=409,
            context={"database": name},
            public_context={
                "code": CODE_SCOPE_NOT_ALLOWED,
                "reason": REASON_SYSTEM_DATABASE,
                "side": side,
            },
        )
    if query_policy.is_gateway_metadata_target(
        host=host,
        port=port,
        database=name,
        gateway_host=DB_HOST,
        gateway_port=DB_PORT,
        gateway_database=DB_NAME,
    ):
        raise AppHttpException(
            message=(
                "Esa base de datos es la propia base de metadatos del gateway: no se puede "
                "usar como origen ni como destino de esta operación."
            ),
            status_code=409,
            context={"database": name},
            public_context={
                "code": CODE_SCOPE_NOT_ALLOWED,
                "reason": REASON_GATEWAY_METADATA,
                "side": side,
            },
        )
