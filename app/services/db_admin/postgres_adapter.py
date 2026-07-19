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

from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.core.remote_engine import database_connection, map_driver_error, server_connection
from app.exceptions import AppHttpException
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import (
    DumpStatement,
    EngineUserInfo,
    EnumTypeInfo,
    ExtensionInfo,
    GrantInfo,
    GrantLevel,
    ObjectRef,
    RoutineInfo,
    SequenceInfo,
    StructureDump,
    TriggerInfo,
    ViewInfo,
)
from app.services.db_admin.identifiers import (
    quote_identifier,
    quote_string_literal,
    validate_identifier,
)


class PostgresAdapter(ServerAdapter):
    dialect = "postgresql"
    # Un ROLE de PostgreSQL no tiene host (el acceso por host se controla en
    # pg_hba.conf, fuera del alcance SQL): no hay "agregar host" ni identidades
    # múltiples por username. add_user_host/copy_user_grants heredan el 422 del base.
    supports_hosts = False

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

    # ------------------------- snapshot estructural (Plan 09) ----------------- #
    def dump_structure(self, database: str) -> StructureDump:
        """
        Dump estructural de una BD PostgreSQL (schema ``public``).

        PostgreSQL no tiene ``SHOW CREATE``: las tablas se reconstruyen por reflexión
        de SQLAlchemy (``CreateTable``) y el resto vía ``pg_get_*def()`` y catálogos.
        Orden de dependencia: extensiones → tipos → secuencias → tablas → índices →
        vistas → vistas materializadas → rutinas → triggers. Cada bloque opcional
        degrada con gracia si la feature no aplica. Solo estructura, nunca filas.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        statements: list[DumpStatement] = []
        has_non_portable = False

        def _safe(conn, sql, params=None):
            """Ejecuta una consulta de catálogo OPCIONAL; [] si la feature no existe."""
            try:
                return conn.execute(text(sql), params or {}).fetchall()
            except SQLAlchemyError:
                return []

        try:
            with database_connection(self.target, database) as conn:
                # 1) Extensiones (plpgsql viene por defecto: se omite).
                for (extname,) in _safe(
                    conn,
                    "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' "
                    "ORDER BY extname",
                ):
                    ext = quote_identifier(extname, self.dialect)
                    statements.append(
                        DumpStatement(
                            object_type="extension",
                            name=extname,
                            ddl=f"CREATE EXTENSION IF NOT EXISTS {ext}",
                        )
                    )

                # 2) Tipos ENUM definidos por el usuario.
                for typname, labels in _safe(
                    conn,
                    "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) "
                    "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public' GROUP BY t.typname ORDER BY t.typname",
                ):
                    name_q = quote_identifier(typname, self.dialect)
                    vals = ", ".join(
                        quote_string_literal(lbl, self.dialect) for lbl in labels
                    )
                    statements.append(
                        DumpStatement(
                            object_type="type",
                            name=typname,
                            ddl=f"CREATE TYPE {name_q} AS ENUM ({vals})",
                        )
                    )

                # 3) Secuencias STANDALONE (no las creadas por columnas serial/identity).
                for (seqname,) in _safe(
                    conn,
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'S' AND n.nspname = 'public' "
                    "AND NOT EXISTS (SELECT 1 FROM pg_depend d "
                    "                WHERE d.objid = c.oid AND d.deptype = 'a') "
                    "ORDER BY c.relname",
                ):
                    seq_q = quote_identifier(seqname, self.dialect)
                    statements.append(
                        DumpStatement(
                            object_type="sequence",
                            name=seqname,
                            ddl=f"CREATE SEQUENCE IF NOT EXISTS {seq_q}",
                        )
                    )

                # 4) Tablas (reflexión + compilador CreateTable, en dialecto PG).
                insp = inspect(conn)
                table_names = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                            "ORDER BY table_name"
                        )
                    ).fetchall()
                ]
                for tname in table_names:
                    validate_identifier(tname, self.dialect, "tabla", allow_existing=True)
                    tbl = Table(tname, MetaData(), autoload_with=conn, schema="public")
                    ddl = str(CreateTable(tbl).compile(conn.engine)).strip()
                    referred = sorted(
                        {
                            fk["referred_table"]
                            for fk in insp.get_foreign_keys(tname, schema="public")
                            if fk.get("referred_table") and fk["referred_table"] != tname
                        }
                    )
                    statements.append(
                        DumpStatement(
                            object_type="table", name=tname, ddl=ddl, depends_on=referred
                        )
                    )

                # 5) Índices NO respaldados por una constraint (PK/UNIQUE ya van en la tabla).
                for idxname, idxdef, idxtable in _safe(
                    conn,
                    "SELECT i.indexname, i.indexdef, i.tablename FROM pg_indexes i "
                    "WHERE i.schemaname = 'public' AND NOT EXISTS "
                    "(SELECT 1 FROM pg_constraint c WHERE c.conname = i.indexname) "
                    "ORDER BY i.indexname",
                ):
                    statements.append(
                        DumpStatement(
                            object_type="index", name=idxname, ddl=idxdef,
                            depends_on=[idxtable] if idxtable else [],
                        )
                    )

                # 6) Vistas.
                for vname, vdef in _safe(
                    conn,
                    "SELECT table_name, view_definition FROM information_schema.views "
                    "WHERE table_schema = 'public' ORDER BY table_name",
                ):
                    name_q = quote_identifier(vname, self.dialect)
                    statements.append(
                        DumpStatement(
                            object_type="view",
                            name=vname,
                            ddl=f"CREATE VIEW {name_q} AS {vdef}",
                        )
                    )

                # 7) Vistas materializadas.
                for mname, mdef in _safe(
                    conn,
                    "SELECT matviewname, definition FROM pg_matviews "
                    "WHERE schemaname = 'public' ORDER BY matviewname",
                ):
                    name_q = quote_identifier(mname, self.dialect)
                    statements.append(
                        DumpStatement(
                            object_type="materialized_view",
                            name=mname,
                            ddl=f"CREATE MATERIALIZED VIEW {name_q} AS {mdef}",
                        )
                    )

                # 8) Funciones y procedures (pg_get_functiondef). NO portables.
                #    Se captura proname (rutinas homónimas/overloads comparten nombre).
                for proname, fdef in _safe(
                    conn,
                    "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p') "
                    "ORDER BY p.proname",
                ):
                    has_non_portable = True
                    statements.append(
                        DumpStatement(object_type="routine", name=proname or "", ddl=fdef)
                    )

                # 9) Triggers (pg_get_triggerdef, depends_on = tabla). NO portables.
                for tgname, tgdef, on_table in _safe(
                    conn,
                    "SELECT t.tgname, pg_get_triggerdef(t.oid), c.relname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
                    "ORDER BY t.tgname",
                ):
                    has_non_portable = True
                    statements.append(
                        DumpStatement(
                            object_type="trigger", name=tgname, ddl=tgdef,
                            depends_on=[on_table] if on_table else [],
                        )
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

    def revoke_object(self, grantee, level, object_ref, privileges, *, cascade=False) -> None:
        stmt, _on, server_level = self._build_dcl("REVOKE", grantee, level, object_ref, privileges)
        if cascade:
            stmt += " CASCADE"  # default del motor es RESTRICT
        extra = {"username": grantee.username, "level": level.value}
        if server_level:
            self._execute_server([stmt], op="revoke_object", extra=extra)
        else:
            db = self._require_field(object_ref.database, "database")
            self._execute_database(db, [stmt], op="revoke_object", extra=extra)

    _LIST_GRANTS_SQL = (
        "SELECT 'table' AS lvl, table_schema || '.' || table_name AS obj, privilege_type AS p, is_grantable AS g "
        "  FROM information_schema.role_table_grants WHERE grantee = :g "
        "UNION ALL SELECT 'column', table_schema || '.' || table_name || '(' || column_name || ')', "
        "  privilege_type, is_grantable FROM information_schema.role_column_grants WHERE grantee = :g "
        "UNION ALL SELECT 'routine', routine_schema || '.' || routine_name, privilege_type, is_grantable "
        "  FROM information_schema.role_routine_grants WHERE grantee = :g "
        "UNION ALL SELECT 'sequence', object_schema || '.' || object_name, privilege_type, is_grantable "
        "  FROM information_schema.role_usage_grants WHERE grantee = :g AND object_type = 'SEQUENCE'"
    )

    def list_grants(self, grantee, database=None) -> list[GrantInfo]:
        validate_identifier(grantee.username, self.dialect, "usuario", allow_existing=True)
        if not database:
            raise AppHttpException(
                message="En PostgreSQL se requiere 'database' para listar los grants de objeto.",
                status_code=422,
            )
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        try:
            with database_connection(self.target, database) as conn:
                rows = conn.execute(text(self._LIST_GRANTS_SQL), {"g": grantee.username}).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_grants", target=self.target, extra={"username": grantee.username}
            )
        agg: dict[tuple[str, str], dict] = {}
        for lvl, obj, priv, grantable in rows:
            entry = agg.setdefault((lvl, obj), {"privs": set(), "wgo": False})
            entry["privs"].add(priv)
            if str(grantable).upper() == "YES":
                entry["wgo"] = True
        return [
            GrantInfo(level=GrantLevel(lvl), object=obj, privileges=sorted(e["privs"]), with_grant_option=e["wgo"])
            for (lvl, obj), e in agg.items()
            if e["privs"]
        ]

    # has_*_privilege por nivel (para can_grant de grantors NO superusuario).
    _HAS_FN = {
        GrantLevel.DATABASE: "has_database_privilege",
        GrantLevel.SCHEMA: "has_schema_privilege",
        GrantLevel.TABLE: "has_table_privilege",
        GrantLevel.COLUMN: "has_table_privilege",  # aprox. a nivel tabla
        GrantLevel.SEQUENCE: "has_sequence_privilege",
        GrantLevel.ROUTINE: "has_function_privilege",
    }

    def can_grant(self, level, object_ref, privileges) -> bool:
        canonical, _ = priv_catalog.validate_privileges(privileges, self.dialect, level)
        try:
            with server_connection(self.target) as conn:
                is_super = conn.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                ).scalar()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="can_grant", target=self.target)
        if is_super:
            return True
        # Grantor NO superusuario: exigir el privilegio CON grant option para cada uno.
        fn = self._HAS_FN.get(level)
        if fn is None:
            return False
        if level == GrantLevel.DATABASE:
            obj_expr = self._require_field(object_ref.database, "database")
            runner = server_connection(self.target)
        else:
            obj_expr = self._can_grant_object_name(level, object_ref)
            runner = database_connection(self.target, self._require_field(object_ref.database, "database"))
        checks = [p for p in canonical if p not in ("ALL PRIVILEGES",)]
        try:
            with runner as conn:
                for priv in checks or ["USAGE"]:
                    ok = conn.execute(
                        text(f"SELECT {fn}(current_user, :obj, :priv)"),
                        {"obj": obj_expr, "priv": f"{priv} WITH GRANT OPTION"},
                    ).scalar()
                    if not ok:
                        return False
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="can_grant", target=self.target)
        return True

    def _can_grant_object_name(self, level, ref) -> str:
        """Nombre de objeto para has_*_privilege (validado)."""
        schema = self._require_field(ref.db_schema or "public", "schema")
        validate_identifier(schema, self.dialect, "esquema", allow_existing=True)
        if level == GrantLevel.SCHEMA:
            return schema
        name = {
            GrantLevel.TABLE: ref.table, GrantLevel.COLUMN: ref.table,
            GrantLevel.SEQUENCE: ref.sequence,
            GrantLevel.ROUTINE: ref.routine.name if ref.routine else None,
        }.get(level)
        validate_identifier(self._require_field(name, "objeto"), self.dialect, "objeto", allow_existing=True)
        return f"{schema}.{name}"

    def _estimate_rows(self, conn, table: str, schema: str) -> int:
        row = conn.execute(
            text(
                "SELECT c.reltuples::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = :t AND n.nspname = :s"
            ),
            {"t": table, "s": schema},
        ).scalar()
        return int(row) if row is not None and row >= 0 else 0

    # ------------------------- snapshot canónico (hooks) ---------------------- #
    @staticmethod
    def _safe_fetch(conn, sql, params=None):
        """Consulta de catálogo OPCIONAL: [] si la feature no existe en esta versión."""
        try:
            return conn.execute(text(sql), params or {}).fetchall()
        except SQLAlchemyError:
            return []

    def _column_extras(self, conn, database, table, schema) -> dict[str, dict]:
        # PG: solo collation por columna. information_schema.columns.collation_name es
        # NULL cuando la columna usa la collation por defecto (regla de herencia gratis).
        out: dict[str, dict] = {}
        for name, coll in self._safe_fetch(
            conn,
            "SELECT column_name, collation_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t",
            {"t": table},
        ):
            out[name] = {"collation": coll, "charset": None, "on_update": None}
        return out

    def _snapshot_views(self, conn, database, schema) -> list[ViewInfo]:
        out: list[ViewInfo] = []
        for vname, vdef, check_option in self._safe_fetch(
            conn,
            "SELECT table_name, view_definition, check_option FROM information_schema.views "
            "WHERE table_schema = 'public' ORDER BY table_name",
        ):
            cols = [
                r[0]
                for r in self._safe_fetch(
                    conn,
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position",
                    {"t": vname},
                )
            ]
            out.append(
                ViewInfo(
                    name=vname, is_materialized=False, definition=str(vdef or ""),
                    columns=cols,
                    check_option=None if not check_option or str(check_option) == "NONE" else str(check_option),
                )
            )
        for mname, mdef in self._safe_fetch(
            conn,
            "SELECT matviewname, definition FROM pg_matviews "
            "WHERE schemaname = 'public' ORDER BY matviewname",
        ):
            out.append(ViewInfo(name=mname, is_materialized=True, definition=str(mdef or "")))
        return out

    def _snapshot_routines(self, conn, database, schema) -> list[RoutineInfo]:
        out: list[RoutineInfo] = []
        _vol = {"i": "IMMUTABLE", "s": "STABLE", "v": "VOLATILE"}
        for proname, prokind, fdef, lang, ret, volatile, secdef in self._safe_fetch(
            conn,
            "SELECT p.proname, p.prokind, pg_get_functiondef(p.oid), l.lanname, "
            "pg_get_function_result(p.oid), p.provolatile, p.prosecdef "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p') ORDER BY p.proname",
        ):
            out.append(
                RoutineInfo(
                    name=proname or "",
                    kind="PROCEDURE" if prokind == "p" else "FUNCTION",
                    return_type=str(ret) if ret else None,
                    language=str(lang) if lang else None,
                    volatility=_vol.get(str(volatile), None),
                    security_definer=bool(secdef),
                    body=str(fdef or ""),
                )
            )
        return out

    def _snapshot_triggers(self, conn, database, schema) -> list[TriggerInfo]:
        # La identidad estructural del trigger vive en pg_get_triggerdef (captura timing/
        # eventos/nivel/condición): timing/events/level se dejan None y el diff compara
        # el cuerpo normalizado.
        out: list[TriggerInfo] = []
        for tgname, relname, tgdef in self._safe_fetch(
            conn,
            "SELECT t.tgname, c.relname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY t.tgname",
        ):
            out.append(TriggerInfo(name=tgname, table=relname or "", action=str(tgdef or "")))
        return out

    def _snapshot_sequences(self, conn, database, schema) -> list[SequenceInfo]:
        out: list[SequenceInfo] = []
        for name, dtype, incr, mn, mx, start, cycle in self._safe_fetch(
            conn,
            "SELECT c.relname, s.seqtypid::regtype::text, s.seqincrement, s.seqmin, "
            "s.seqmax, s.seqstart, s.seqcycle FROM pg_sequence s "
            "JOIN pg_class c ON c.oid = s.seqrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT EXISTS "
            "(SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'a') "
            "ORDER BY c.relname",
        ):
            out.append(
                SequenceInfo(
                    name=name, data_type=str(dtype) if dtype else None,
                    increment=incr, min_value=mn, max_value=mx, start_value=start,
                    cycle=bool(cycle),
                )
            )
        return out

    def _snapshot_enum_types(self, conn, database, schema) -> list[EnumTypeInfo]:
        out: list[EnumTypeInfo] = []
        for typname, labels in self._safe_fetch(
            conn,
            "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) "
            "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' GROUP BY t.typname ORDER BY t.typname",
        ):
            out.append(EnumTypeInfo(name=typname, values=[str(v) for v in (labels or [])]))
        return out

    def _snapshot_extensions(self, conn, database, schema) -> list[ExtensionInfo]:
        out: list[ExtensionInfo] = []
        for extname, extversion in self._safe_fetch(
            conn,
            "SELECT extname, extversion FROM pg_extension WHERE extname <> 'plpgsql' "
            "ORDER BY extname",
        ):
            out.append(ExtensionInfo(name=extname, version=str(extversion) if extversion else None))
        return out

    # ------------------------- generación de DDL (Fase 3) --------------------- #
    # Todo NOMBRE de objeto pasa por validate_identifier + quote_identifier (self._q).
    # Los cuerpos de vistas/rutinas/triggers se re-emiten tal cual (pg_get_*def, sin
    # DEFINER) — requieren revisión individual (requires_individual_review).
    def _render_column_def(self, col) -> str:
        parts = [self._q(col.name, "columna"), col.type]
        if col.collation:
            parts.append(f"COLLATE {self._q(col.collation, 'collation')}")
        if col.identity is not None:
            mode = "ALWAYS" if col.identity.always else "BY DEFAULT"
            parts.append(f"GENERATED {mode} AS IDENTITY")
        elif col.computed is not None:
            parts.append(f"GENERATED ALWAYS AS ({col.computed.sqltext}) STORED")
        elif col.default is not None:
            parts.append(f"DEFAULT {col.default}")
        if not col.nullable:
            parts.append("NOT NULL")
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
        return f"CREATE TABLE {self._q(tbl.table, 'tabla')} (\n  {body}\n)"

    def _render_modify_column(self, table, src_col, tgt_col, changed) -> list[str]:
        # PostgreSQL no redefine en una sentencia: una por atributo que cambió.
        t = self._q(table, "tabla")
        c = self._q(src_col.name, "columna")
        stmts: list[str] = []
        if "type" in changed:
            # USING best-effort (conversión no binaria-coercible). El motor de diff ya
            # marcó needs_review para este ítem.
            stmts.append(
                f"ALTER TABLE {t} ALTER COLUMN {c} TYPE {src_col.type} USING {c}::{src_col.type}"
            )
        if "collation" in changed:
            coll = self._q(src_col.collation, "collation") if src_col.collation else '"default"'
            stmts.append(f"ALTER TABLE {t} ALTER COLUMN {c} TYPE {src_col.type} COLLATE {coll}")
        if "nullable" in changed:
            action = "DROP NOT NULL" if src_col.nullable else "SET NOT NULL"
            stmts.append(f"ALTER TABLE {t} ALTER COLUMN {c} {action}")
        if "default" in changed:
            if src_col.default is None:
                stmts.append(f"ALTER TABLE {t} ALTER COLUMN {c} DROP DEFAULT")
            else:
                stmts.append(f"ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT {src_col.default}")
        if "comment" in changed:
            cmt = quote_string_literal(src_col.comment, self.dialect) if src_col.comment else "NULL"
            stmts.append(f"COMMENT ON COLUMN {t}.{c} IS {cmt}")
        return stmts

    def _drop_constraint(self, table: str, name: str | None) -> str:
        if not name:
            raise AppHttpException(
                message="No se puede DROP de una constraint sin nombre en PostgreSQL.",
                status_code=422,
            )
        return f"ALTER TABLE {self._q(table, 'tabla')} DROP CONSTRAINT {self._q(name, 'constraint')}"

    def _render_drop_fk(self, table, fk) -> str:
        return self._drop_constraint(table, fk.name)

    def _render_drop_unique(self, table, uc) -> str:
        return self._drop_constraint(table, uc.name)

    def _render_drop_check(self, table, ck) -> str:
        return self._drop_constraint(table, ck.name)

    def _render_create_index(self, table, ix) -> str:
        unique = "UNIQUE " if ix.unique else ""
        name = (
            self._q(ix.name, "indice") if ix.name
            else self._q(f"ix_{table}_{'_'.join(ix.columns)}"[:63], "indice")
        )
        method = ""
        if ix.method:
            method = f" USING {validate_identifier(ix.method, self.dialect, 'metodo', allow_existing=True)}"
        cols = ", ".join(self._q(c, "columna") for c in ix.columns)
        sql = f"CREATE {unique}INDEX {name} ON {self._q(table, 'tabla')}{method} ({cols})"
        if ix.include_columns:
            sql += " INCLUDE (" + ", ".join(self._q(c, "columna") for c in ix.include_columns) + ")"
        if ix.predicate:
            sql += f" WHERE {ix.predicate}"
        return sql

    def _render_drop_index(self, table, ix) -> str:
        if not ix.name:
            raise AppHttpException(message="No se puede DROP de un índice sin nombre.", status_code=422)
        return f"DROP INDEX {self._q(ix.name, 'indice')}"

    def _render_alter_pk(self, table, src_tbl, tgt_tbl) -> list[str]:
        stmts: list[str] = []
        if tgt_tbl.primary_key:
            pkname = tgt_tbl.primary_key_name or f"{table}_pkey"
            stmts.append(self._drop_constraint(table, pkname))
        if src_tbl.primary_key:
            cols = ", ".join(self._q(c, "columna") for c in src_tbl.primary_key)
            stmts.append(f"ALTER TABLE {self._q(table, 'tabla')} ADD PRIMARY KEY ({cols})")
        return stmts

    def _render_view(self, view, replace) -> list[str]:
        if view.is_materialized:
            stmts: list[str] = []
            if replace:  # matview no soporta OR REPLACE
                stmts.append(f"DROP MATERIALIZED VIEW IF EXISTS {self._q(view.name, 'vista')}")
            stmts.append(f"CREATE MATERIALIZED VIEW {self._q(view.name, 'vista')} AS {view.definition}")
            return stmts
        cols = ""
        if view.columns:
            cols = " (" + ", ".join(self._q(c, "columna") for c in view.columns) + ")"
        sql = f"CREATE OR REPLACE VIEW {self._q(view.name, 'vista')}{cols} AS {view.definition}"
        if view.check_option:
            sql += f" WITH {view.check_option} CHECK OPTION"
        return [sql]

    def _render_drop_view(self, view) -> str:
        kind = "MATERIALIZED VIEW" if view.is_materialized else "VIEW"
        return f"DROP {kind} {self._q(view.name, 'vista')}"

    def _render_routine(self, routine, replace) -> list[str]:
        # pg_get_functiondef ya emite CREATE OR REPLACE FUNCTION/PROCEDURE.
        return [routine.body]

    def _render_drop_routine(self, routine) -> str:
        kind = "PROCEDURE" if routine.kind.upper() == "PROCEDURE" else "FUNCTION"
        # best-effort: sin firma de argumentos (falla ante overloads; se documenta).
        return f"DROP {kind} IF EXISTS {self._q(routine.name, 'rutina')}"

    def _render_trigger(self, trigger, replace) -> list[str]:
        stmts: list[str] = []
        if replace:  # PG <14 no tiene CREATE OR REPLACE TRIGGER -> DROP + CREATE
            stmts.append(
                f"DROP TRIGGER IF EXISTS {self._q(trigger.name, 'trigger')} "
                f"ON {self._q(trigger.table, 'tabla')}"
            )
        stmts.append(trigger.action)
        return stmts

    def _render_drop_trigger(self, trigger) -> str:
        return (
            f"DROP TRIGGER {self._q(trigger.name, 'trigger')} "
            f"ON {self._q(trigger.table, 'tabla')}"
        )

    def _render_sequence(self, seq, *, alter) -> list[str]:
        verb = "ALTER" if alter else "CREATE"
        sql = f"{verb} SEQUENCE {self._q(seq.name, 'secuencia')}"
        if seq.increment is not None:
            sql += f" INCREMENT BY {int(seq.increment)}"
        if seq.min_value is not None:
            sql += f" MINVALUE {int(seq.min_value)}"
        if seq.max_value is not None:
            sql += f" MAXVALUE {int(seq.max_value)}"
        sql += " CYCLE" if seq.cycle else " NO CYCLE"
        return [sql]

    def _render_enum(self, src_enum, tgt_enum) -> list[str]:
        q = self._q(src_enum.name, "tipo")
        if tgt_enum is None:
            vals = ", ".join(quote_string_literal(v, self.dialect) for v in src_enum.values)
            return [f"CREATE TYPE {q} AS ENUM ({vals})"]
        # modified: solo la parte ADITIVA (ADD VALUE). Quitar/reordenar valores exige
        # recrear el tipo y las columnas dependientes -> queda a revisión del operador.
        existing = set(tgt_enum.values)
        return [
            f"ALTER TYPE {q} ADD VALUE {quote_string_literal(v, self.dialect)}"
            for v in src_enum.values if v not in existing
        ]
