"""
Adaptador para MySQL y MariaDB.

Particularidades:
- Los usuarios se identifican por el par `'usuario'@'host'`.
- Los permisos se otorgan a nivel de BD entera con `ON `db`.*`.
- No existe "owner" nativo de schema: la propiedad es un concepto lógico que el
  gateway mantiene en su BD de metadatos, respaldado por `GRANT ALL ON db.*`.
"""

from sqlalchemy import text

from app.core.remote_engine import map_driver_error, server_connection
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import EngineUserInfo
from app.services.db_admin.identifiers import (
    quote_identifier,
    quote_string_literal,
    validate_host,
    validate_identifier,
    validate_privileges,
)
from sqlalchemy.exc import SQLAlchemyError

_SYSTEM_DATABASES = ("information_schema", "mysql", "performance_schema", "sys")
_SYSTEM_USERS = (
    "mysql.sys",
    "mysql.session",
    "mysql.infoschema",
    "root",
    "mariadb.sys",
    "debian-sys-maint",
)


def _in_list(values: tuple[str, ...]) -> str:
    """Construye una lista IN (...) a partir de CONSTANTES internas (no input)."""
    return ", ".join("'" + v + "'" for v in values)


class MySQLAdapter(ServerAdapter):
    dialect = "mysql"

    def _version_sql(self) -> str:
        return "SELECT VERSION()"

    def _inspect_schema(self, database: str) -> str:
        # Conectados a la BD, el Inspector usa el schema = nombre de la BD.
        return database

    def list_databases(self) -> list[str]:
        sql = (
            "SELECT SCHEMA_NAME AS name FROM INFORMATION_SCHEMA.SCHEMATA "
            f"WHERE SCHEMA_NAME NOT IN ({_in_list(_SYSTEM_DATABASES)}) "
            "ORDER BY SCHEMA_NAME"
        )
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(sql)).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="list_databases", target=self.target)
        return [r.name for r in rows]

    def list_users(self) -> list[EngineUserInfo]:
        sql = (
            "SELECT User AS username, Host AS host FROM mysql.user "
            f"WHERE User NOT IN ({_in_list(_SYSTEM_USERS)}) "
            "ORDER BY User, Host"
        )
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(sql)).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="list_users", target=self.target)
        return [EngineUserInfo(username=r.username, host=r.host) for r in rows]

    # ------------------------- escritura (Iteración 2) ------------------------ #
    def create_database(
        self, db_name, charset=None, collation=None, owner=None
    ) -> None:
        validate_identifier(db_name, self.dialect, "base de datos")
        charset = validate_identifier(charset or "utf8mb4", self.dialect, "charset")
        db = quote_identifier(db_name, self.dialect)
        sql = f"CREATE DATABASE {db} CHARACTER SET {charset}"
        if collation:
            validate_identifier(collation, self.dialect, "collation")
            sql += f" COLLATE {collation}"
        self._execute_server([sql], op="create_database", extra={"database": db_name})

    def drop_database(self, db_name) -> None:
        validate_identifier(db_name, self.dialect, "base de datos")
        db = quote_identifier(db_name, self.dialect)
        self._execute_server(
            [f"DROP DATABASE {db}"], op="drop_database", extra={"database": db_name}
        )

    def create_user(self, username, password, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_host(host)
        pwd = quote_string_literal(password, self.dialect)
        self._execute_server(
            [f"CREATE USER '{username}'@'{host}' IDENTIFIED BY {pwd}"],
            op="create_user",
            extra={"username": username},
        )

    def drop_user(self, username, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_host(host)
        self._execute_server(
            [f"DROP USER '{username}'@'{host}'"],
            op="drop_user",
            extra={"username": username},
        )

    def change_password(self, username, new_password, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_host(host)
        pwd = quote_string_literal(new_password, self.dialect)
        self._execute_server(
            [f"ALTER USER '{username}'@'{host}' IDENTIFIED BY {pwd}"],
            op="change_password",
            extra={"username": username},
        )

    def grant_database(self, username, db_name, host="%", privileges="ALL PRIVILEGES") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_identifier(db_name, self.dialect, "base de datos")
        validate_host(host)
        privs = validate_privileges(privileges)
        db = quote_identifier(db_name, self.dialect)
        self._execute_server(
            [
                f"GRANT {privs} ON {db}.* TO '{username}'@'{host}'",
                "FLUSH PRIVILEGES",
            ],
            op="grant_database",
            extra={"username": username, "database": db_name},
        )

    def revoke_database(self, username, db_name, host="%", privileges="ALL PRIVILEGES") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_identifier(db_name, self.dialect, "base de datos")
        validate_host(host)
        privs = validate_privileges(privileges)
        db = quote_identifier(db_name, self.dialect)
        self._execute_server(
            [
                f"REVOKE {privs} ON {db}.* FROM '{username}'@'{host}'",
                "FLUSH PRIVILEGES",
            ],
            op="revoke_database",
            extra={"username": username, "database": db_name},
        )


class MariaDBAdapter(MySQLAdapter):
    dialect = "mariadb"
