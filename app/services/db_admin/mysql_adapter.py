"""
Adaptador para MySQL y MariaDB.

Particularidades:
- Los usuarios se identifican por el par `'usuario'@'host'`.
- Los permisos se otorgan a nivel de BD entera con `ON `db`.*`.
- No existe "owner" nativo de schema: la propiedad es un concepto lógico que el
  gateway mantiene en su BD de metadatos, respaldado por `GRANT ALL ON db.*`.
"""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.remote_engine import map_driver_error, server_connection
from app.exceptions import AppHttpException
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import EngineUserInfo, GrantInfo, GrantLevel, ObjectRef
from app.services.db_admin.identifiers import (
    quote_identifier,
    quote_string_literal,
    validate_host,
    validate_identifier,
    validate_privileges,
)

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

    def _user_at_host(self, username: str, host: str) -> str:
        """
        Construye el identificador ``'user'@'host'`` de MySQL con DOBLE defensa:
        validación por whitelist (arriba) Y quoting como string literal (aquí). En
        MySQL ambas partes son string literals, así que se escapan con
        ``quote_string_literal`` en vez de comillas manuales (nunca confiar solo en
        la whitelist; ver app/services/db_admin/identifiers.py).
        """
        user_lit = quote_string_literal(username, self.dialect)
        host_lit = quote_string_literal(host, self.dialect)
        return f"{user_lit}@{host_lit}"

    def create_user(self, username, password, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_host(host)
        pwd = quote_string_literal(password, self.dialect)
        self._execute_server(
            [f"CREATE USER {self._user_at_host(username, host)} IDENTIFIED BY {pwd}"],
            op="create_user",
            extra={"username": username},
        )

    def drop_user(self, username, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_host(host)
        self._execute_server(
            [f"DROP USER {self._user_at_host(username, host)}"],
            op="drop_user",
            extra={"username": username},
        )

    def change_password(self, username, new_password, host="%") -> None:
        validate_identifier(username, self.dialect, "usuario")
        validate_host(host)
        pwd = quote_string_literal(new_password, self.dialect)
        self._execute_server(
            [f"ALTER USER {self._user_at_host(username, host)} IDENTIFIED BY {pwd}"],
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
                f"GRANT {privs} ON {db}.* TO {self._user_at_host(username, host)}",
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
                f"REVOKE {privs} ON {db}.* FROM {self._user_at_host(username, host)}",
                "FLUSH PRIVILEGES",
            ],
            op="revoke_database",
            extra={"username": username, "database": db_name},
        )

    # reassign_database_owner: usa la implementación por defecto del base
    # (revoke al anterior + grant al nuevo), correcta para MySQL/MariaDB.

    # ------------------------- GRANT/REVOKE granular -------------------------- #
    def _object_clause(
        self, level: GrantLevel, ref: ObjectRef, canonical: list[str]
    ) -> tuple[str, str]:
        """
        Construye ``(priv_clause, on_target)`` para MySQL/MariaDB. Los identificadores
        del objeto son PREEXISTENTES (allow_existing) y se quotean; los privilegios
        vienen del catálogo cerrado (constantes) y se interpolan tal cual.
        """
        d = self.dialect

        def q(value: str, kind: str) -> str:
            return quote_identifier(
                validate_identifier(value, d, kind, allow_existing=True), d
            )

        if level == GrantLevel.DATABASE:
            db = q(self._require_field(ref.database, "database"), "base de datos")
            return ", ".join(canonical), f"{db}.*"
        if level in (GrantLevel.TABLE, GrantLevel.COLUMN):
            db = q(self._require_field(ref.database, "database"), "base de datos")
            tbl = q(self._require_field(ref.table, "table"), "tabla")
            target = f"{db}.{tbl}"
            if level == GrantLevel.TABLE:
                return ", ".join(canonical), target
            # COLUMN: cada privilegio lleva la lista de columnas (validadas una a una).
            if not ref.columns:
                raise AppHttpException(
                    message="Se requieren columnas para un permiso a nivel columna.",
                    status_code=422,
                )
            col_list = "(" + ", ".join(q(c, "columna") for c in ref.columns) + ")"
            return ", ".join(f"{p} {col_list}" for p in canonical), target
        if level == GrantLevel.ROUTINE:
            db = q(self._require_field(ref.database, "database"), "base de datos")
            kind = self._routine_kind(ref.routine)
            fn = q(self._require_field(ref.routine.name, "routine.name"), "rutina")
            return ", ".join(canonical), f"{kind} {db}.{fn}"
        raise AppHttpException(
            message="Nivel de permiso no soportado para este motor.",
            status_code=422,
            context={"level": level.value, "dialect": d},
        )

    def _grantee(self, grantee: EngineUserInfo) -> str:
        validate_identifier(grantee.username, self.dialect, "usuario", allow_existing=True)
        host = grantee.host or "%"
        validate_host(host)
        return self._user_at_host(grantee.username, host)

    def grant_object(
        self, grantee, level, object_ref, privileges, *, with_grant_option=False
    ) -> None:
        canonical, _ = priv_catalog.validate_privileges(privileges, self.dialect, level)
        # "GRANT OPTION" se confiere con la cláusula WITH GRANT OPTION, no como
        # privilegio en sí (`GRANT GRANT OPTION ...` sería inválido). Si queda vacío,
        # se usa USAGE (otorga la grant option sin otros privilegios).
        wgo = with_grant_option
        privs = [p for p in canonical if p != "GRANT OPTION"]
        if "GRANT OPTION" in canonical:
            wgo = True
        if not privs:
            privs = ["USAGE"]
        priv_clause, on_target = self._object_clause(level, object_ref, privs)
        stmt = f"GRANT {priv_clause} ON {on_target} TO {self._grantee(grantee)}"
        if wgo:
            stmt += " WITH GRANT OPTION"
        self._execute_server(
            [stmt], op="grant_object", extra={"username": grantee.username, "level": level.value}
        )

    def revoke_object(self, grantee, level, object_ref, privileges, *, cascade=False) -> None:
        if cascade:
            raise AppHttpException(
                message="MySQL/MariaDB no soporta REVOKE ... CASCADE.",
                status_code=422,
                context={"dialect": self.dialect},
            )
        canonical, _ = priv_catalog.validate_privileges(privileges, self.dialect, level)
        priv_clause, on_target = self._object_clause(level, object_ref, canonical)
        stmt = f"REVOKE {priv_clause} ON {on_target} FROM {self._grantee(grantee)}"
        self._execute_server(
            [stmt], op="revoke_object", extra={"username": grantee.username, "level": level.value}
        )

    _LIST_GRANTS_SQL = (
        "SELECT 'global' AS lvl, NULL AS obj, PRIVILEGE_TYPE AS p, IS_GRANTABLE AS g "
        "  FROM information_schema.USER_PRIVILEGES WHERE GRANTEE = :g "
        "UNION ALL SELECT 'database', TABLE_SCHEMA, PRIVILEGE_TYPE, IS_GRANTABLE "
        "  FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE = :g "
        "UNION ALL SELECT 'table', CONCAT(TABLE_SCHEMA, '.', TABLE_NAME), PRIVILEGE_TYPE, IS_GRANTABLE "
        "  FROM information_schema.TABLE_PRIVILEGES WHERE GRANTEE = :g "
        "UNION ALL SELECT 'column', CONCAT(TABLE_SCHEMA, '.', TABLE_NAME, '(', COLUMN_NAME, ')'), "
        "  PRIVILEGE_TYPE, IS_GRANTABLE FROM information_schema.COLUMN_PRIVILEGES WHERE GRANTEE = :g"
    )

    def list_grants(self, grantee, database=None) -> list[GrantInfo]:
        validate_identifier(grantee.username, self.dialect, "usuario", allow_existing=True)
        host = grantee.host or "%"
        validate_host(host)
        grantee_lit = f"'{grantee.username}'@'{host}'"
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(self._LIST_GRANTS_SQL), {"g": grantee_lit}).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_grants", target=self.target, extra={"username": grantee.username}
            )
        agg: dict[tuple[str, str | None], dict] = {}
        for lvl, obj, priv, grantable in rows:
            entry = agg.setdefault((lvl, obj), {"privs": set(), "wgo": False})
            # USAGE = "sin privilegios"; no es informativo en un listado.
            if priv != "USAGE":
                entry["privs"].add(priv)
            if str(grantable).upper() == "YES":
                entry["wgo"] = True
        return [
            GrantInfo(level=GrantLevel(lvl), object=obj, privileges=sorted(e["privs"]), with_grant_option=e["wgo"])
            for (lvl, obj), e in agg.items()
            if e["privs"]
        ]

    def can_grant(self, level, object_ref, privileges) -> bool:
        canonical, _ = priv_catalog.validate_privileges(privileges, self.dialect, level)
        # Privilegios GRANTABLES del grantor (CURRENT_USER) a nivel GLOBAL — cubre la
        # credencial pseudo-root. Conservador para grantors limitados (refuerzo: el
        # error del motor es la red secundaria al ejecutar).
        sql = text(
            "SELECT PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES "
            "WHERE GRANTEE = CONCAT(QUOTE(SUBSTRING_INDEX(CURRENT_USER(), '@', 1)), '@', "
            "QUOTE(SUBSTRING_INDEX(CURRENT_USER(), '@', -1))) AND IS_GRANTABLE = 'YES'"
        )
        try:
            with server_connection(self.target) as conn:
                grantable = {r[0].upper() for r in conn.execute(sql)}
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="can_grant", target=self.target)
        if "ALL PRIVILEGES" in canonical:
            # Delegar ALL PRIVILEGES requiere IS_GRANTABLE='YES' en algo (grantable no vacío).
            return bool(grantable)
        needed = {p for p in canonical if p not in ("GRANT OPTION", "USAGE")}
        if "GRANT OPTION" in canonical and not grantable:
            # "GRANT OPTION" nunca aparece como PRIVILEGE_TYPE; tener grantable vacío
            # significa que no se puede delegar grant option.
            return False
        return needed.issubset(grantable)


class MariaDBAdapter(MySQLAdapter):
    dialect = "mariadb"
