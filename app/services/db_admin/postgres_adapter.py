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
from app.exceptions import AppHttpException
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import EngineUserInfo, GrantLevel, ObjectRef
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

    # ------------------------- GRANT/REVOKE granular -------------------------- #
    def _qualified(self, ref: ObjectRef, name: str, kind: str) -> str:
        """``"schema"."objeto"`` (schema default 'public'). Identificadores preexistentes."""
        schema = self._require_field(ref.db_schema or "public", "schema")
        validate_identifier(schema, self.dialect, "esquema", allow_existing=True)
        validate_identifier(name, self.dialect, kind, allow_existing=True)
        return (
            f"{quote_identifier(schema, self.dialect)}."
            f"{quote_identifier(name, self.dialect)}"
        )

    def _object_clause(
        self, level: GrantLevel, ref: ObjectRef, canonical: list[str]
    ) -> tuple[str, str, bool]:
        """
        Devuelve ``(priv_clause, on_target, server_level)``. ``server_level=True`` →
        ejecutar a nivel servidor (DATABASE); en otro caso, conectado a la BD del ref.
        """
        d = self.dialect

        def q(value: str, kind: str) -> str:
            return quote_identifier(
                validate_identifier(value, d, kind, allow_existing=True), d
            )

        if level == GrantLevel.DATABASE:
            db = q(self._require_field(ref.database, "database"), "base de datos")
            return ", ".join(canonical), f"DATABASE {db}", True
        if level == GrantLevel.SCHEMA:
            s = q(self._require_field(ref.db_schema or "public", "schema"), "esquema")
            return ", ".join(canonical), f"SCHEMA {s}", False
        if level in (GrantLevel.TABLE, GrantLevel.COLUMN):
            target = self._qualified(ref, self._require_field(ref.table, "table"), "tabla")
            if level == GrantLevel.TABLE:
                return ", ".join(canonical), f"TABLE {target}", False
            if not ref.columns:
                raise AppHttpException(
                    message="Se requieren columnas para un permiso a nivel columna.",
                    status_code=422,
                )
            col_list = "(" + ", ".join(q(c, "columna") for c in ref.columns) + ")"
            return ", ".join(f"{p} {col_list}" for p in canonical), target, False
        if level == GrantLevel.SEQUENCE:
            target = self._qualified(ref, self._require_field(ref.sequence, "sequence"), "secuencia")
            return ", ".join(canonical), f"SEQUENCE {target}", False
        if level == GrantLevel.ROUTINE:
            kind = self._routine_kind(ref.routine)
            target = self._qualified(ref, self._require_field(ref.routine.name, "routine.name"), "rutina")
            return ", ".join(canonical), f"{kind} {target}", False
        raise AppHttpException(
            message="Nivel de permiso no soportado para este motor.",
            status_code=422,
            context={"level": level.value, "dialect": d},
        )

    def _build_dcl(self, verb: str, grantee, level, ref, privileges) -> tuple[str, str, bool]:
        canonical, _ = priv_catalog.validate_privileges(privileges, self.dialect, level)
        priv_clause, on_target, server_level = self._object_clause(level, ref, canonical)
        role = quote_identifier(
            validate_identifier(grantee.username, self.dialect, "usuario", allow_existing=True),
            self.dialect,
        )
        connector = "TO" if verb == "GRANT" else "FROM"
        return f"{verb} {priv_clause} ON {on_target} {connector} {role}", on_target, server_level

    def grant_object(
        self, grantee, level, object_ref, privileges, *, with_grant_option=False
    ) -> None:
        stmt, _on, server_level = self._build_dcl("GRANT", grantee, level, object_ref, privileges)
        if with_grant_option:
            stmt += " WITH GRANT OPTION"
        extra = {"username": grantee.username, "level": level.value}
        if server_level:
            self._execute_server([stmt], op="grant_object", extra=extra)
        else:
            db = self._require_field(object_ref.database, "database")
            self._execute_database(db, [stmt], op="grant_object", extra=extra)

    def revoke_object(self, grantee, level, object_ref, privileges) -> None:
        stmt, _on, server_level = self._build_dcl("REVOKE", grantee, level, object_ref, privileges)
        extra = {"username": grantee.username, "level": level.value}
        if server_level:
            self._execute_server([stmt], op="revoke_object", extra=extra)
        else:
            db = self._require_field(object_ref.database, "database")
            self._execute_database(db, [stmt], op="revoke_object", extra=extra)
