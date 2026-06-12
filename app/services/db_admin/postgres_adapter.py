"""
Adaptador para PostgreSQL.

Particularidades frente a MySQL:
- Los "usuarios" son ROLES con atributo LOGIN; no hay par usuario@host.
- Una "base de datos" no es un schema: los schemas (`public`, ...) viven dentro.
- La propiedad es NATIVA: `ALTER DATABASE ... OWNER TO ...` (fuente de verdad en el
  motor, a diferencia de MySQL donde es lógica en los metadatos del gateway).
- Otorgar acceso requiere DOS niveles: `GRANT CONNECT ON DATABASE` (a nivel
  servidor) y `GRANT USAGE/ALL ... ON SCHEMA/TABLES` (conectado a la BD).
- `CREATE/DROP DATABASE` exigen AUTOCOMMIT (ya garantizado por server_connection).
"""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.remote_engine import map_driver_error, server_connection
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import EngineUserInfo
from app.services.db_admin.identifiers import (
    quote_identifier,
    quote_string_literal,
    validate_identifier,
)


class PostgresAdapter(ServerAdapter):
    dialect = "postgresql"

    def _version_sql(self) -> str:
        return "SELECT version()"

    def _inspect_schema(self, database: str) -> str:
        return "public"

    def list_databases(self) -> list[str]:
        sql = (
            "SELECT datname AS name FROM pg_database "
            "WHERE datistemplate = false AND datname <> 'postgres' "
            "ORDER BY datname"
        )
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(sql)).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="list_databases", target=self.target)
        return [r.name for r in rows]

    def list_users(self) -> list[EngineUserInfo]:
        sql = (
            "SELECT rolname AS username FROM pg_roles "
            "WHERE rolcanlogin = true AND rolname NOT LIKE 'pg\\_%' ESCAPE '\\' "
            "ORDER BY rolname"
        )
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(sql)).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="list_users", target=self.target)
        return [EngineUserInfo(username=r.username, host=None) for r in rows]

    # ------------------------- escritura (Iteración 2) ------------------------ #
    def create_database(
        self, db_name, charset=None, collation=None, owner=None
    ) -> None:
        validate_identifier(db_name, self.dialect, "base de datos")
        db = quote_identifier(db_name, self.dialect)
        sql = f"CREATE DATABASE {db}"
        if owner:
            validate_identifier(owner, self.dialect, "usuario")
            sql += f" OWNER {quote_identifier(owner, self.dialect)}"
        sql += " ENCODING 'UTF8' TEMPLATE template0"
        self._execute_server([sql], op="create_database", extra={"database": db_name})

    def drop_database(self, db_name) -> None:
        validate_identifier(db_name, self.dialect, "base de datos")
        db = quote_identifier(db_name, self.dialect)
        self._execute_server(
            [f"DROP DATABASE {db}"], op="drop_database", extra={"database": db_name}
        )

    def create_user(self, username, password, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        role = quote_identifier(username, self.dialect)
        pwd = quote_string_literal(password, self.dialect)
        self._execute_server(
            [f"CREATE ROLE {role} WITH LOGIN PASSWORD {pwd}"],
            op="create_user",
            extra={"username": username},
        )

    def drop_user(self, username, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        role = quote_identifier(username, self.dialect)
        self._execute_server(
            [f"DROP ROLE {role}"], op="drop_user", extra={"username": username}
        )

    def change_password(self, username, new_password, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        role = quote_identifier(username, self.dialect)
        pwd = quote_string_literal(new_password, self.dialect)
        self._execute_server(
            [f"ALTER ROLE {role} WITH PASSWORD {pwd}"],
            op="change_password",
            extra={"username": username},
        )

    def grant_database(self, username, db_name, host="%", privileges="ALL PRIVILEGES") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_identifier(db_name, self.dialect, "base de datos")
        role = quote_identifier(username, self.dialect)
        db = quote_identifier(db_name, self.dialect)
        # Nivel servidor: poder conectarse a la BD.
        self._execute_server(
            [f"GRANT CONNECT ON DATABASE {db} TO {role}"],
            op="grant_database",
            extra={"username": username, "database": db_name},
        )
        # Nivel BD: acceso a schema public, tablas existentes y futuras.
        self._execute_database(
            db_name,
            [
                f"GRANT USAGE, CREATE ON SCHEMA public TO {role}",
                f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {role}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT ALL PRIVILEGES ON TABLES TO {role}",
            ],
            op="grant_database",
            extra={"username": username, "database": db_name},
        )

    def revoke_database(self, username, db_name, host="%", privileges="ALL PRIVILEGES") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_identifier(db_name, self.dialect, "base de datos")
        role = quote_identifier(username, self.dialect)
        db = quote_identifier(db_name, self.dialect)
        self._execute_database(
            db_name,
            [
                f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}",
                f"REVOKE USAGE, CREATE ON SCHEMA public FROM {role}",
            ],
            op="revoke_database",
            extra={"username": username, "database": db_name},
        )
        self._execute_server(
            [f"REVOKE CONNECT ON DATABASE {db} FROM {role}"],
            op="revoke_database",
            extra={"username": username, "database": db_name},
        )

    def reassign_database_owner(
        self, db_name, new_owner, *, new_host="%", old_owner=None, old_host="%"
    ) -> None:
        # En PostgreSQL la propiedad es NATIVA: ALTER DATABASE ... OWNER TO ...
        validate_identifier(db_name, self.dialect, "base de datos")
        validate_identifier(new_owner, self.dialect, "usuario")
        db = quote_identifier(db_name, self.dialect)
        role = quote_identifier(new_owner, self.dialect)
        self._execute_server(
            [f"ALTER DATABASE {db} OWNER TO {role}"],
            op="reassign_database_owner",
            extra={"database": db_name, "new_owner": new_owner},
        )
        # Otorgar al nuevo dueño el acceso de dos niveles (CONNECT + schema/tablas).
        self.grant_database(new_owner, db_name)
        # Revocar el acceso del anterior (la propiedad nativa ya cambió arriba).
        if old_owner:
            self.revoke_database(old_owner, db_name)
