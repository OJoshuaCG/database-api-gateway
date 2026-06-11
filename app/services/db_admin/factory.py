"""Selección del adaptador según el dialecto del servidor destino."""

from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.mysql_adapter import MariaDBAdapter, MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter

_ADAPTERS: dict[str, type[ServerAdapter]] = {
    "mysql": MySQLAdapter,
    "mariadb": MariaDBAdapter,
    "postgresql": PostgresAdapter,
}


def get_adapter(target: ServerTarget) -> ServerAdapter:
    cls = _ADAPTERS.get(target.dialect)
    if cls is None:
        raise AppHttpException(
            message=f"Motor de base de datos no soportado: {target.dialect}",
            status_code=422,
            context={"dialect": target.dialect, "supported": list(_ADAPTERS)},
        )
    return cls(target)
