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

from app.core.remote_engine import (
    database_connection,
    map_driver_error,
    server_connection,
)
from app.exceptions import AppHttpException
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import (
    DumpStatement,
    EngineUserInfo,
    EventInfo,
    GrantInfo,
    GrantLevel,
    ObjectRef,
    RoutineInfo,
    RoutineParam,
    StructureDump,
    TriggerInfo,
    ViewInfo,
)
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

    # ------------------------- snapshot estructural (Plan 09) ----------------- #
    @staticmethod
    def _show_create_value(row, candidates: tuple[str, ...], fallback_idx: int) -> str:
        """Extrae el DDL de una fila de SHOW CREATE por nombre de columna (o índice)."""
        mapping = row._mapping
        for key in candidates:
            if key in mapping:
                return mapping[key]
        return row[fallback_idx]

    def dump_structure(self, database: str) -> StructureDump:
        """
        Dump estructural de una BD MySQL/MariaDB vía ``SHOW CREATE *``.

        Orden de dependencia: tablas → vistas → rutinas → triggers → events. El
        ``DEFINER`` se sanea (ver base). Solo estructura, nunca filas.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        statements: list[DumpStatement] = []
        has_non_portable = False
        try:
            with database_connection(self.target, database) as conn:
                # Aristas FK entre tablas (una sola consulta) para depends_on / topo-sort.
                fk_map: dict[str, set[str]] = {}
                for tname, referred in conn.execute(
                    text(
                        "SELECT TABLE_NAME, REFERENCED_TABLE_NAME "
                        "FROM information_schema.KEY_COLUMN_USAGE "
                        "WHERE TABLE_SCHEMA = :db AND REFERENCED_TABLE_NAME IS NOT NULL"
                    ),
                    {"db": database},
                ).fetchall():
                    if referred and referred != tname:
                        fk_map.setdefault(tname, set()).add(referred)

                # 1) Tablas base (no vistas).
                tables = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT TABLE_NAME FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE' "
                            "ORDER BY TABLE_NAME"
                        ),
                        {"db": database},
                    ).fetchall()
                ]
                for t in tables:
                    q = quote_identifier(
                        validate_identifier(t, self.dialect, "tabla", allow_existing=True),
                        self.dialect,
                    )
                    row = conn.execute(text(f"SHOW CREATE TABLE {q}")).fetchone()
                    ddl = self._show_create_value(row, ("Create Table",), 1)
                    statements.append(
                        DumpStatement(
                            object_type="table", name=t, ddl=ddl,
                            depends_on=sorted(fk_map.get(t, set())),
                        )
                    )

                # 2) Vistas.
                views = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT TABLE_NAME FROM information_schema.VIEWS "
                            "WHERE TABLE_SCHEMA = :db ORDER BY TABLE_NAME"
                        ),
                        {"db": database},
                    ).fetchall()
                ]
                for v in views:
                    q = quote_identifier(
                        validate_identifier(v, self.dialect, "vista", allow_existing=True),
                        self.dialect,
                    )
                    row = conn.execute(text(f"SHOW CREATE VIEW {q}")).fetchone()
                    ddl = self._strip_definer_clause(
                        self._show_create_value(row, ("Create View",), 1)
                    )
                    statements.append(DumpStatement(object_type="view", name=v, ddl=ddl))

                # 3) Rutinas (procedures + functions).
                routines = conn.execute(
                    text(
                        "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
                        "WHERE ROUTINE_SCHEMA = :db ORDER BY ROUTINE_TYPE, ROUTINE_NAME"
                    ),
                    {"db": database},
                ).fetchall()
                for name, rtype in routines:
                    kind = "PROCEDURE" if str(rtype).upper() == "PROCEDURE" else "FUNCTION"
                    q = quote_identifier(
                        validate_identifier(name, self.dialect, "rutina", allow_existing=True),
                        self.dialect,
                    )
                    row = conn.execute(text(f"SHOW CREATE {kind} {q}")).fetchone()
                    ddl = self._strip_definer_clause(
                        self._show_create_value(
                            row, (f"Create {kind.capitalize()}",), 2
                        )
                    )
                    has_non_portable = True
                    statements.append(
                        DumpStatement(object_type="routine", name=name, ddl=ddl)
                    )

                # 4) Triggers (depends_on = tabla sobre la que se define).
                triggers = [
                    (r[0], r[1])
                    for r in conn.execute(
                        text(
                            "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE "
                            "FROM information_schema.TRIGGERS "
                            "WHERE TRIGGER_SCHEMA = :db ORDER BY TRIGGER_NAME"
                        ),
                        {"db": database},
                    ).fetchall()
                ]
                for trg, on_table in triggers:
                    q = quote_identifier(
                        validate_identifier(trg, self.dialect, "trigger", allow_existing=True),
                        self.dialect,
                    )
                    row = conn.execute(text(f"SHOW CREATE TRIGGER {q}")).fetchone()
                    ddl = self._strip_definer_clause(
                        self._show_create_value(row, ("SQL Original Statement",), 2)
                    )
                    has_non_portable = True
                    statements.append(
                        DumpStatement(
                            object_type="trigger", name=trg, ddl=ddl,
                            depends_on=[on_table] if on_table else [],
                        )
                    )

                # 5) Events (scheduler). information_schema.EVENTS puede no existir en
                #    instalaciones mínimas; se ignora si la consulta falla.
                try:
                    events = [
                        r[0]
                        for r in conn.execute(
                            text(
                                "SELECT EVENT_NAME FROM information_schema.EVENTS "
                                "WHERE EVENT_SCHEMA = :db ORDER BY EVENT_NAME"
                            ),
                            {"db": database},
                        ).fetchall()
                    ]
                except SQLAlchemyError:
                    events = []
                for ev in events:
                    q = quote_identifier(
                        validate_identifier(ev, self.dialect, "event", allow_existing=True),
                        self.dialect,
                    )
                    row = conn.execute(text(f"SHOW CREATE EVENT {q}")).fetchone()
                    ddl = self._strip_definer_clause(
                        self._show_create_value(row, ("Create Event",), 3)
                    )
                    has_non_portable = True
                    statements.append(
                        DumpStatement(object_type="event", name=ev, ddl=ddl)
                    )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="dump_structure", target=self.target, extra={"database": database}
            )

        return StructureDump(
            database=database,
            source_engine=self.dialect,
            statements=statements,
            has_non_portable=has_non_portable,
        )

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

    def _estimate_rows(self, conn, table: str, schema: str) -> int:
        row = conn.execute(
            text(
                "SELECT TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"
            ),
            {"s": schema, "t": table},
        ).scalar()
        return int(row) if row is not None else 0

    # ------------------------- snapshot canónico (hooks) ---------------------- #
    def _column_extras(self, conn, database, table, schema) -> dict[str, dict]:
        """
        Collation/charset/on_update por columna desde ``information_schema.COLUMNS``:
        el Inspector de SQLAlchemy no expone estos de forma fiable en MySQL/MariaDB.
        """
        out: dict[str, dict] = {}
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME, COLLATION_NAME, CHARACTER_SET_NAME, EXTRA "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t"
            ),
            {"db": database, "t": table},
        ).fetchall()
        for name, coll, cs, extra in rows:
            on_update = None
            if extra and "on update" in str(extra).lower():
                on_update = "CURRENT_TIMESTAMP"
            out[name] = {
                "collation": coll,
                "charset": cs,
                "on_update": on_update,
            }
        return out

    def _table_storage_options(self, conn, database, table, schema) -> dict[str, str]:
        opts: dict[str, str] = {}
        row = conn.execute(
            text(
                "SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t"
            ),
            {"db": database, "t": table},
        ).fetchone()
        if row:
            if row[0]:
                opts["engine"] = str(row[0])
            if row[1]:
                opts["collation"] = str(row[1])
                opts["charset"] = str(row[1]).split("_", 1)[0]
        db_row = conn.execute(
            text(
                "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :db"
            ),
            {"db": database},
        ).fetchone()
        if db_row:
            if db_row[0]:
                opts["db_charset"] = str(db_row[0])
            if db_row[1]:
                opts["db_collation"] = str(db_row[1])
        return opts

    def _snapshot_views(self, conn, database, schema) -> list[ViewInfo]:
        # Se guarda el SELECT (VIEW_DEFINITION) — no el SHOW CREATE completo — para poder
        # re-emitir un CREATE OR REPLACE VIEW controlado (mismo formato que PostgreSQL).
        out: list[ViewInfo] = []
        rows = conn.execute(
            text(
                "SELECT TABLE_NAME, VIEW_DEFINITION, CHECK_OPTION, SECURITY_TYPE "
                "FROM information_schema.VIEWS WHERE TABLE_SCHEMA = :db ORDER BY TABLE_NAME"
            ),
            {"db": database},
        ).fetchall()
        for name, vdef, check_option, security in rows:
            cols = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
                    ),
                    {"db": database, "t": name},
                ).fetchall()
            ]
            out.append(
                ViewInfo(
                    name=name,
                    is_materialized=False,
                    definition=str(vdef or ""),
                    columns=cols,
                    check_option=None if not check_option or check_option == "NONE" else str(check_option),
                    security_definer=str(security or "").upper() == "DEFINER",
                )
            )
        return out

    def _snapshot_routines(self, conn, database, schema) -> list[RoutineInfo]:
        out: list[RoutineInfo] = []
        rows = conn.execute(
            text(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE, DTD_IDENTIFIER, IS_DETERMINISTIC, "
                "SECURITY_TYPE FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA = :db ORDER BY ROUTINE_TYPE, ROUTINE_NAME"
            ),
            {"db": database},
        ).fetchall()
        for name, rtype, return_type, deterministic, security in rows:
            kind = "PROCEDURE" if str(rtype).upper() == "PROCEDURE" else "FUNCTION"
            q = quote_identifier(
                validate_identifier(name, self.dialect, "rutina", allow_existing=True),
                self.dialect,
            )
            crow = conn.execute(text(f"SHOW CREATE {kind} {q}")).fetchone()
            body = self._strip_definer_clause(
                self._show_create_value(crow, (f"Create {kind.capitalize()}",), 2)
            )
            params: list[RoutineParam] = []
            for pname, pmode, dtd, ordinal in conn.execute(
                text(
                    "SELECT PARAMETER_NAME, PARAMETER_MODE, DTD_IDENTIFIER, ORDINAL_POSITION "
                    "FROM information_schema.PARAMETERS "
                    "WHERE SPECIFIC_SCHEMA = :db AND SPECIFIC_NAME = :n ORDER BY ORDINAL_POSITION"
                ),
                {"db": database, "n": name},
            ).fetchall():
                if ordinal == 0:  # posición 0 = tipo de retorno de una FUNCTION
                    continue
                params.append(RoutineParam(name=pname, mode=pmode, type=str(dtd or "")))
            out.append(
                RoutineInfo(
                    name=name,
                    kind=kind,
                    parameters=params,
                    return_type=str(return_type) if return_type else None,
                    language="SQL",
                    deterministic=str(deterministic or "").upper() == "YES",
                    security_definer=str(security or "").upper() == "DEFINER",
                    body=body,
                )
            )
        return out

    def _snapshot_triggers(self, conn, database, schema) -> list[TriggerInfo]:
        out: list[TriggerInfo] = []
        rows = conn.execute(
            text(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, "
                "EVENT_MANIPULATION, ACTION_ORIENTATION "
                "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = :db ORDER BY TRIGGER_NAME"
            ),
            {"db": database},
        ).fetchall()
        for name, tbl, timing, event, orientation in rows:
            q = quote_identifier(
                validate_identifier(name, self.dialect, "trigger", allow_existing=True),
                self.dialect,
            )
            crow = conn.execute(text(f"SHOW CREATE TRIGGER {q}")).fetchone()
            action = self._strip_definer_clause(
                self._show_create_value(crow, ("SQL Original Statement",), 2)
            )
            out.append(
                TriggerInfo(
                    name=name,
                    table=tbl or "",
                    timing=str(timing) if timing else None,
                    events=[str(event)] if event else [],
                    level=str(orientation) if orientation else None,
                    action=action,
                )
            )
        return out

    def _snapshot_events(self, conn, database, schema) -> list[EventInfo]:
        try:
            rows = conn.execute(
                text(
                    "SELECT EVENT_NAME FROM information_schema.EVENTS "
                    "WHERE EVENT_SCHEMA = :db ORDER BY EVENT_NAME"
                ),
                {"db": database},
            ).fetchall()
        except SQLAlchemyError:
            return []
        out: list[EventInfo] = []
        for (name,) in rows:
            q = quote_identifier(
                validate_identifier(name, self.dialect, "event", allow_existing=True),
                self.dialect,
            )
            crow = conn.execute(text(f"SHOW CREATE EVENT {q}")).fetchone()
            body = self._strip_definer_clause(self._show_create_value(crow, ("Create Event",), 3))
            out.append(EventInfo(name=name, body=body))
        return out


    # ------------------------- generación de DDL (Fase 3) --------------------- #
    # NOTA: los type strings (col.type) provienen de introspección y se emiten
    # verbatim (no son identificadores). Todo NOMBRE de objeto pasa por
    # validate_identifier + quote_identifier (self._q). Cuerpos de vistas/rutinas/
    # triggers/events se re-emiten tal cual (DEFINER ya saneado) — requieren revisión
    # individual del operador (requires_individual_review).
    def _render_column_def(self, col) -> str:
        parts = [self._q(col.name, "columna"), col.type]
        if col.charset:
            parts.append(
                f"CHARACTER SET {validate_identifier(col.charset, self.dialect, 'charset', allow_existing=True)}"
            )
        if col.collation:
            parts.append(
                f"COLLATE {validate_identifier(col.collation, self.dialect, 'collation', allow_existing=True)}"
            )
        if col.computed is not None:
            stored = "STORED" if col.computed.persisted else "VIRTUAL"
            parts.append(f"GENERATED ALWAYS AS ({col.computed.sqltext}) {stored}")
            if not col.nullable:
                parts.append("NOT NULL")
        else:
            parts.append("NULL" if col.nullable else "NOT NULL")
            if col.default is not None:
                parts.append(f"DEFAULT {col.default}")
            if col.on_update:
                parts.append("ON UPDATE CURRENT_TIMESTAMP")
            if col.autoincrement:
                parts.append("AUTO_INCREMENT")
        if col.comment:
            parts.append(f"COMMENT {quote_string_literal(col.comment, self.dialect)}")
        return " ".join(parts)

    def _render_create_table(self, tbl) -> str:
        lines = [self._render_column_def(c) for c in tbl.columns]
        if tbl.primary_key:
            pk = ", ".join(self._q(c, "columna") for c in tbl.primary_key)
            lines.append(f"PRIMARY KEY ({pk})")
        for uc in tbl.unique_constraints:
            cols = ", ".join(self._q(c, "columna") for c in uc.columns)
            name = f"CONSTRAINT {self._q(uc.name, 'constraint')} " if uc.name else ""
            lines.append(f"{name}UNIQUE ({cols})")
        for ck in tbl.check_constraints:
            name = f"CONSTRAINT {self._q(ck.name, 'constraint')} " if ck.name else ""
            lines.append(f"{name}CHECK ({ck.sqltext})")
        body = ",\n  ".join(lines)
        sql = f"CREATE TABLE {self._q(tbl.table, 'tabla')} (\n  {body}\n)"
        opts = tbl.storage_options
        if opts.get("engine"):
            sql += f" ENGINE={validate_identifier(opts['engine'], self.dialect, 'engine', allow_existing=True)}"
        if opts.get("charset"):
            sql += f" DEFAULT CHARSET={validate_identifier(opts['charset'], self.dialect, 'charset', allow_existing=True)}"
        if opts.get("collation"):
            sql += f" COLLATE={validate_identifier(opts['collation'], self.dialect, 'collation', allow_existing=True)}"
        return sql

    def _render_modify_column(self, table, src_col, tgt_col, changed) -> list[str]:
        # MySQL: una sola MODIFY COLUMN con la definición COMPLETA del estado destino
        # (omitir NOT NULL/DEFAULT/COMMENT los revertiría al default — gotcha del plan).
        return [
            f"ALTER TABLE {self._q(table, 'tabla')} MODIFY COLUMN {self._render_column_def(src_col)}"
        ]

    def _render_drop_fk(self, table, fk) -> str:
        if not fk.name:
            raise AppHttpException(
                message="No se puede DROP de una FK sin nombre en MySQL/MariaDB.",
                status_code=422,
            )
        return f"ALTER TABLE {self._q(table, 'tabla')} DROP FOREIGN KEY {self._q(fk.name, 'constraint')}"

    def _render_drop_unique(self, table, uc) -> str:
        if not uc.name:
            raise AppHttpException(
                message="No se puede DROP de una UNIQUE sin nombre en MySQL/MariaDB.",
                status_code=422,
            )
        return f"ALTER TABLE {self._q(table, 'tabla')} DROP INDEX {self._q(uc.name, 'constraint')}"

    def _render_drop_check(self, table, ck) -> str:
        # MySQL 8: DROP CHECK. MariaDB usa DROP CONSTRAINT (override en MariaDBAdapter).
        if not ck.name:
            raise AppHttpException(
                message="No se puede DROP de un CHECK sin nombre.", status_code=422
            )
        return f"ALTER TABLE {self._q(table, 'tabla')} DROP CHECK {self._q(ck.name, 'constraint')}"

    def _render_create_index(self, table, ix) -> str:
        unique = "UNIQUE " if ix.unique else ""
        cols = ", ".join(self._q(c, "columna") for c in ix.columns)
        name = self._q(ix.name, "indice") if ix.name else self._q(f"ix_{table}_{'_'.join(ix.columns)}"[:64], "indice")
        sql = f"CREATE {unique}INDEX {name} ON {self._q(table, 'tabla')} ({cols})"
        if ix.method:
            sql += f" USING {validate_identifier(ix.method, self.dialect, 'metodo', allow_existing=True)}"
        return sql

    def _render_drop_index(self, table, ix) -> str:
        if not ix.name:
            raise AppHttpException(message="No se puede DROP de un índice sin nombre.", status_code=422)
        return f"DROP INDEX {self._q(ix.name, 'indice')} ON {self._q(table, 'tabla')}"

    def _render_alter_pk(self, table, src_tbl, tgt_tbl) -> list[str]:
        stmts: list[str] = []
        if tgt_tbl.primary_key:
            stmts.append(f"ALTER TABLE {self._q(table, 'tabla')} DROP PRIMARY KEY")
        if src_tbl.primary_key:
            cols = ", ".join(self._q(c, "columna") for c in src_tbl.primary_key)
            stmts.append(f"ALTER TABLE {self._q(table, 'tabla')} ADD PRIMARY KEY ({cols})")
        return stmts

    def _render_view(self, view, replace) -> list[str]:
        # MySQL/MariaDB: CREATE OR REPLACE VIEW cubre new y modified.
        cols = ""
        if view.columns:
            cols = " (" + ", ".join(self._q(c, "columna") for c in view.columns) + ")"
        sql = f"CREATE OR REPLACE VIEW {self._q(view.name, 'vista')}{cols} AS {view.definition}"
        if view.check_option:
            sql += f" WITH {view.check_option} CHECK OPTION"
        return [sql]

    def _render_drop_view(self, view) -> str:
        return f"DROP VIEW {self._q(view.name, 'vista')}"

    def _render_routine(self, routine, replace) -> list[str]:
        # MySQL no tiene CREATE OR REPLACE para rutinas -> DROP + CREATE en 'modified'.
        stmts: list[str] = []
        kind = "PROCEDURE" if routine.kind.upper() == "PROCEDURE" else "FUNCTION"
        if replace:
            stmts.append(f"DROP {kind} IF EXISTS {self._q(routine.name, 'rutina')}")
        stmts.append(routine.body)  # CREATE completo, DEFINER ya saneado
        return stmts

    def _render_drop_routine(self, routine) -> str:
        kind = "PROCEDURE" if routine.kind.upper() == "PROCEDURE" else "FUNCTION"
        return f"DROP {kind} {self._q(routine.name, 'rutina')}"

    def _render_trigger(self, trigger, replace) -> list[str]:
        stmts: list[str] = []
        if replace:  # MySQL no tiene CREATE OR REPLACE TRIGGER
            stmts.append(f"DROP TRIGGER IF EXISTS {self._q(trigger.name, 'trigger')}")
        stmts.append(trigger.action)  # CREATE TRIGGER completo, DEFINER ya saneado
        return stmts

    def _render_drop_trigger(self, trigger) -> str:
        return f"DROP TRIGGER {self._q(trigger.name, 'trigger')}"

    def _render_event(self, event, replace) -> list[str]:
        stmts: list[str] = []
        if replace:
            stmts.append(f"DROP EVENT IF EXISTS {self._q(event.name, 'event')}")
        stmts.append(event.body)
        return stmts


class MariaDBAdapter(MySQLAdapter):
    dialect = "mariadb"

    def _render_drop_check(self, table, ck) -> str:
        # MariaDB elimina CHECK con DROP CONSTRAINT (no DROP CHECK como MySQL 8).
        if not ck.name:
            raise AppHttpException(
                message="No se puede DROP de un CHECK sin nombre.", status_code=422
            )
        return f"ALTER TABLE {self._q(table, 'tabla')} DROP CONSTRAINT {self._q(ck.name, 'constraint')}"
