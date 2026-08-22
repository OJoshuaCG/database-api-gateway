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

import hashlib
import re

from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.core.remote_engine import database_connection, map_driver_error, server_connection
from app.exceptions import AppHttpException
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import (
    CollatableForeignKey,
    CollationGroup,
    CollationInventory,
    CollationOptionInfo,
    ColumnCollationInfo,
    DatabaseGranteeInfo,
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
    TableCollationInfo,
    TriggerInfo,
    ViewInfo,
)
from app.services.db_admin.identifiers import (
    exclude_gateway_internal_tables,
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

    def export_supported_types(self) -> frozenset[str]:
        """
        El núcleo común + lo que solo existe acá: vistas materializadas, secuencias
        autónomas, tipos ENUM de catálogo y extensiones.

        ``event`` NO está: PostgreSQL no tiene scheduler propio (pg_cron es una extensión
        externa y sus tareas no son objetos del esquema).
        """
        return super().export_supported_types() | {
            "materialized_view",
            "sequence",
            "enum_type",
            "extension",
        }

    # ---- Emisión del artefacto de exportación (§7) ----------------------- #
    # PostgreSQL sí arrastra dependencias con CASCADE (y es la única forma de soltar una
    # tabla de la que cuelga una vista). MySQL/MariaDB aceptan la palabra pero la ignoran.
    _EXPORT_SUPPORTS_CASCADE = True

    # ``IF NOT EXISTS`` disponible desde PG 9.5 para estos tipos. ``CREATE TYPE`` y
    # ``CREATE TRIGGER`` NO lo tienen en ninguna versión.
    _EXPORT_IF_NOT_EXISTS_TYPES = frozenset(
        {"table", "sequence", "extension", "index", "materialized_view"}
    )
    # Vistas (``CREATE OR REPLACE VIEW``) y rutinas (``pg_get_functiondef`` emite
    # ``CREATE OR REPLACE FUNCTION/PROCEDURE``) ya salen idempotentes del renderer.
    _EXPORT_ALREADY_IDEMPOTENT_TYPES = frozenset({"view", "routine"})

    # A los tipos no ordenables comunes se suman los GEOMÉTRICOS de PostgreSQL, que
    # directamente no tienen operador de orden (``could not identify an ordering operator``):
    # incluirlos en el ORDER BY de respaldo de una tabla sin PK haría fallar la consulta,
    # no solo perder determinismo.
    _EXPORT_UNORDERABLE_TYPE_TOKENS = (
        ServerAdapter._EXPORT_UNORDERABLE_TYPE_TOKENS
        + ("point", "polygon", "lseg", "box", "path", "circle", "line")
    )

    def export_scope_ddl(
        self, database, mode, *, charset=None, collation=None, if_exists=True
    ) -> list[str]:
        """
        ``CREATE/DROP DATABASE`` de PostgreSQL.

        Dos avisos que el preview ya publica y que acá se materializan en el texto: ninguna
        de las dos sentencias es ejecutable desde una conexión a ESA misma base, ni dentro
        de un bloque transaccional. Quien ejecute el artefacto tiene que estar conectado a
        otra base (típicamente ``postgres``).
        """
        mode = str(mode)
        if mode == "NONE":
            return []
        db = self._q(database, "base de datos")
        out: list[str] = []
        if mode == "DROP_CREATE":
            out.append(f"DROP DATABASE {'IF EXISTS ' if if_exists else ''}{db}")
        if mode == "CREATE_IF_NOT_EXISTS":
            # PostgreSQL NO tiene ``CREATE DATABASE IF NOT EXISTS`` (ninguna versión).
            # La matriz de compatibilidad ya lo rechaza con un 422 accionable ANTES de
            # llegar acá; esto es defensa en profundidad para que un camino nuevo no emita
            # en silencio un script con un error de sintaxis en su segunda línea.
            raise AppHttpException(
                message=(
                    "PostgreSQL no admite CREATE DATABASE IF NOT EXISTS: usá "
                    "structure.scope_ddl='CREATE' o 'NONE'."
                ),
                status_code=422,
                public_context={
                    "code": "export.incompatible_option",
                    "field": "structure.scope_ddl",
                    "engine": self.dialect,
                    "allowed": ["NONE", "CREATE", "DROP_CREATE"],
                },
            )
        parts = [f"CREATE DATABASE {db}"]
        # ``charset`` → ENCODING (LITERAL de string, no identificador); ``collation`` es el
        # LOCALE del SO y fija LC_COLLATE/LC_CTYPE. Mismo criterio que ``create_database``.
        if charset:
            parts.append(f"ENCODING {quote_string_literal(charset, self.dialect)}")
        if collation:
            loc = quote_string_literal(collation, self.dialect)
            parts.append(f"LC_COLLATE {loc} LC_CTYPE {loc}")
        if charset or collation:
            # TEMPLATE template0 es REQUERIDO para fijar encoding/locale distintos del default.
            parts.append("TEMPLATE template0")
        out.append(" ".join(parts))
        return out

    def _export_drop_suffix(self, object_type: str, payload) -> list[str]:
        """``DROP TRIGGER x ON tabla``: en PostgreSQL el trigger pertenece a su tabla."""
        if object_type == "trigger" and payload is not None:
            return ["ON", self._q(payload.table, "tabla")]
        return []

    def export_session_preamble(
        self, *, charset=None, collation=None, suspend_constraints=True
    ) -> list[str]:
        """
        Preámbulo al estilo ``pg_dump``, y deliberadamente SIN
        ``SET session_replication_role = 'replica'``.

        Esa variable es la única forma de suspender FKs y triggers en PostgreSQL, pero
        **exige superusuario**: emitirla haría abortar el script (con ``ON_ERROR_STOP``) o
        dejar un error confuso en la salida para cualquier operador normal. Y no hace falta:
        el default del módulo es ``constraints_placement='deferred'``, que emite índices y
        FKs DESPUÉS de los datos, así que el problema de orden que la variable resolvería ya
        está resuelto por construcción. Se documenta como hueco conocido del §7.1 en vez de
        emitir algo que falla en el caso común.

        ``check_function_bodies = false`` sí va: sin él, crear una función que referencia
        una tabla que todavía no existe falla en la validación del cuerpo.
        """
        out = [
            "SET client_encoding = "
            f"{quote_string_literal(charset or 'UTF8', self.dialect)}",
            "SET standard_conforming_strings = on",
            "SET check_function_bodies = false",
        ]
        return out

    def export_session_epilogue(self) -> list[str]:
        """
        ``RESET`` en vez de fijar valores: devuelve cada parámetro al que tenía la sesión al
        conectarse (``postgresql.conf`` / ``ALTER ROLE``). Fijar un valor "por defecto"
        supuesto pisaría la configuración de quien ejecuta el script.
        """
        return [
            "RESET check_function_bodies",
            "RESET standard_conforming_strings",
            "RESET client_encoding",
            "RESET search_path",
        ]

    def export_use_scope(self, database: str) -> str | None:
        """
        PostgreSQL no tiene ``USE``: el ámbito exportable es el schema ``public`` (misma
        limitación que el diff, el clon y la conversión de collation), y el contexto se fija
        con ``search_path``. La base la elige quien se conecta, no el script.
        """
        return "SET search_path TO \"public\""

    def export_insert_wrapper(
        self, table, columns, *, variant="insert", primary_key=()
    ) -> tuple[str, str]:
        q = self._q(table, "tabla")
        cols = self._export_column_list(columns)
        prefix = f"INSERT INTO {q}{cols} VALUES"
        if variant in ("insert", "none"):
            return prefix, ""
        if variant == "insert_ignore":
            # Sin lista de conflicto: cubre CUALQUIER restricción única, que es la semántica
            # de ``INSERT IGNORE`` de MySQL.
            return prefix, " ON CONFLICT DO NOTHING"
        if variant == "upsert":
            updatable = [c for c in columns if c not in set(primary_key)]
            if not primary_key or not columns or not updatable:
                # ``ON CONFLICT DO UPDATE`` EXIGE nombrar el conflicto: sin PK no hay
                # destino de conflicto que declarar y la sentencia no es construible.
                raise self._export_unsupported_variant("upsert")
            conflict = ", ".join(self._q(c, "columna") for c in primary_key)
            assignments = ", ".join(
                f"{self._q(c, 'columna')} = EXCLUDED.{self._q(c, 'columna')}"
                for c in updatable
            )
            return prefix, f" ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
        # ``replace`` (DELETE+INSERT implícito de MySQL) no tiene equivalente en PostgreSQL:
        # emularlo con DELETE previo cambiaría el significado sin que el usuario lo pidiera.
        raise self._export_unsupported_variant(variant)

    def export_counter_reset(
        self, table: str, value: int | None, *, column: str | None = None
    ) -> str | None:
        """
        En PostgreSQL el contador vive en una SECUENCIA, no en la tabla.

        Se resuelve con ``pg_get_serial_sequence`` en vez de construir el nombre a mano
        (``t_id_seq``): ese nombre depende de cómo se creó la columna —``serial``,
        ``IDENTITY`` o una secuencia asociada con ``OWNED BY``— y adivinarlo produce
        ``relation … does not exist`` en cuanto alguien renombró la tabla. ``setval`` es
        STRICT: si la columna no tiene secuencia detrás, ``pg_get_serial_sequence`` devuelve
        NULL y la llamada entera devuelve NULL sin error.

        Sin ``column`` no se emite nada: inventar cuál es la columna del contador es
        exactamente el tipo de suposición que deja un artefacto roto.
        """
        if value is None or not column:
            return None
        validate_identifier(table, self.dialect, "tabla", allow_existing=True)
        validate_identifier(column, self.dialect, "columna", allow_existing=True)
        # Argumentos de ``pg_get_serial_sequence``: LITERALES de texto, no identificadores.
        qualified = quote_string_literal(f"public.{table}", self.dialect)
        col = quote_string_literal(column, self.dialect)
        return (
            f"SELECT setval(pg_get_serial_sequence({qualified}, {col}), "
            f"{int(value)}, true)"
        )

    def export_counter_value_sql(
        self, database: str, table: str, column: str
    ) -> tuple[str, dict] | None:
        """
        Último valor entregado por la secuencia que respalda la columna.

        Coincide con la semántica de ``setval(seq, n, true)`` que emite
        ``export_counter_reset`` (fija el ÚLTIMO usado, el próximo será ``n+1``). La
        secuencia se resuelve con ``pg_get_serial_sequence`` en vez de construir el nombre a
        mano, por el mismo motivo que allá: el nombre depende de cómo se creó la columna.

        ``pg_sequence_last_value`` devuelve NULL si la secuencia nunca se usó, y
        ``pg_get_serial_sequence`` devuelve NULL si la columna no tiene secuencia detrás; en
        ambos casos el resultado es NULL y no se emite ningún ajuste. ``database`` no se usa:
        la conexión ya está en esa base y el módulo cubre solo el schema ``public``.
        """
        return (
            "SELECT pg_catalog.pg_sequence_last_value("
            "pg_get_serial_sequence(:qualified, :col))",
            {"qualified": f"public.{table}", "col": column},
        )

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
    def dump_structure(self, database: str, *, conn=None) -> StructureDump:
        """
        Dump estructural de una BD PostgreSQL (schema ``public``).

        PostgreSQL no tiene ``SHOW CREATE``: las tablas se reconstruyen por reflexión
        de SQLAlchemy (``CreateTable``) y el resto vía ``pg_get_*def()`` y catálogos.
        Orden de dependencia: extensiones → tipos → secuencias → tablas → índices →
        vistas → vistas materializadas → rutinas → triggers. Cada bloque opcional
        degrada con gracia si la feature no aplica. Solo estructura, nunca filas.

        ``conn`` (§6.4 del módulo de exportación): ver ``ServerAdapter._conn_ctx``.
        ``None`` = comportamiento histórico (conexión propia).
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
            with self._conn_ctx(database, conn) as conn:
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
                # Se excluye la contabilidad interna del gateway (``_gw_v_*``/
                # ``_gw_stg_*``): ver el mismo filtro en MySQLAdapter.dump_structure.
                table_names = exclude_gateway_internal_tables(
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                            "ORDER BY table_name"
                        )
                    ).fetchall()
                )
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

    def _database_defaults(self, conn, database, schema) -> dict[str, str | None]:
        """Default de encoding/locale de la BD desde ``pg_database`` (best-effort).

        ``db_charset`` = nombre de encoding (p.ej. ``UTF8``); ``db_collation`` = locale
        ``LC_COLLATE`` (p.ej. ``en_US.UTF-8``). Ambos alimentan el ``CREATE DATABASE`` de un
        clon PG→PG; en un clon cross-engine el llamador los descarta.
        """
        row = conn.execute(
            text(
                "SELECT pg_encoding_to_char(encoding), datcollate "
                "FROM pg_database WHERE datname = :db"
            ),
            {"db": database},
        ).fetchone()
        if not row:
            return {}
        return {
            "db_charset": str(row[0]) if row[0] else None,
            "db_collation": str(row[1]) if row[1] else None,
        }

    # ------------------------- escritura (Iteración 2) ------------------------ #
    def create_database(
        self, db_name, charset=None, collation=None, owner=None
    ) -> None:
        validate_identifier(db_name, self.dialect, "base de datos")
        db = quote_identifier(db_name, self.dialect)
        parts = [f"CREATE DATABASE {db}"]
        if owner:
            validate_identifier(owner, self.dialect, "usuario")
            parts.append(f"OWNER {quote_identifier(owner, self.dialect)}")
        # ``charset`` → ENCODING (default UTF8). Va como LITERAL de string, no identificador.
        encoding = charset or "UTF8"
        parts.append(f"ENCODING {quote_string_literal(encoding, self.dialect)}")
        # ``collation`` es el LOCALE de la BD (p.ej. 'en_US.UTF-8'); fija LC_COLLATE y LC_CTYPE.
        # CAVEAT OPERATIVO: el locale DEBE existir en el SO del servidor PostgreSQL, o el
        # CREATE DATABASE falla con "invalid locale name". Solo se emite si el llamador lo pide.
        if collation:
            loc = quote_string_literal(collation, self.dialect)
            parts.append(f"LC_COLLATE {loc} LC_CTYPE {loc}")
        # TEMPLATE template0 es REQUERIDO por PG para fijar encoding/locale distintos del default.
        parts.append("TEMPLATE template0")
        self._execute_server(
            [" ".join(parts)], op="create_database", extra={"database": db_name}
        )

    def drop_database(self, db_name, *, force_disconnect=False) -> None:
        # ``allow_existing``: la BD puede tener un nombre legado que la whitelist estricta
        # rechaza. PostgreSQL RECHAZA el DROP si hay sesiones abiertas contra la BD
        # ("database is being accessed by other users"); con ``force_disconnect`` se
        # terminan primero. La terminación corre en la sesión de NIVEL SERVIDOR (conectada
        # a ``postgres``, no a la BD que se borra) y excluye el propio backend. Funciona en
        # todas las versiones (a diferencia de ``WITH (FORCE)``, que es PG 13+). Queda una
        # ventana de carrera mínima (una conexión nueva entre terminar y dropear); el motor
        # es la red secundaria si eso ocurre.
        validate_identifier(db_name, self.dialect, "base de datos", allow_existing=True)
        db = quote_identifier(db_name, self.dialect)
        if force_disconnect:
            try:
                with server_connection(self.target) as conn:
                    conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :db AND pid <> pg_backend_pid()"
                        ),
                        {"db": db_name},
                    )
            except SQLAlchemyError as exc:
                raise map_driver_error(
                    exc, op="drop_database", target=self.target, extra={"database": db_name}
                )
        self._execute_server(
            [f"DROP DATABASE {db}"], op="drop_database", extra={"database": db_name}
        )

    def active_connections(self, db_name) -> int:
        validate_identifier(db_name, self.dialect, "base de datos", allow_existing=True)
        sql = (
            "SELECT COUNT(*) FROM pg_stat_activity "
            "WHERE datname = :db AND pid <> pg_backend_pid()"
        )
        try:
            with server_connection(self.target) as conn:
                return int(conn.execute(text(sql), {"db": db_name}).scalar() or 0)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="active_connections", target=self.target, extra={"database": db_name}
            )

    def list_database_grantees(self, db_name) -> list[DatabaseGranteeInfo]:
        # DOS niveles (como grant_database): (1) nivel servidor — owner de la BD + ACL de
        # ``pg_database.datacl`` (CONNECT/CREATE/TEMP), la señal PRIMARIA de "quién tiene
        # relación con la BD"; (2) nivel BD — grants de objeto del schema public (enriquece,
        # best-effort). Las vistas ``role_*_grants`` NO cubren CONNECT, por eso se lee datacl.
        validate_identifier(db_name, self.dialect, "base de datos", allow_existing=True)
        agg: dict[str, dict] = {}

        def _add(username: str | None, priv: str | None, level: str) -> None:
            if not username or username.startswith("pg_"):
                return
            entry = agg.setdefault(
                username, {"privs": set(), "levels": set()}
            )
            if priv:
                entry["privs"].add(priv)
            entry["levels"].add(level)

        # (1) Nivel servidor: owner + datacl.
        try:
            with server_connection(self.target) as conn:
                owner = conn.execute(
                    text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :db"),
                    {"db": db_name},
                ).scalar()
                if owner:
                    _add(owner, "OWNER", "database")
                rows = conn.execute(
                    text(
                        "SELECT r.rolname AS grantee, a.privilege_type AS p "
                        "FROM pg_database d "
                        "CROSS JOIN LATERAL aclexplode(d.datacl) a "
                        "JOIN pg_roles r ON r.oid = a.grantee "
                        "WHERE d.datname = :db"
                    ),
                    {"db": db_name},
                ).fetchall()
                for grantee, priv in rows:
                    _add(grantee, priv, "database")
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_database_grantees", target=self.target,
                extra={"database": db_name},
            )

        # (2) Nivel BD: grants de tabla del schema public (best-effort; enriquece).
        try:
            with database_connection(self.target, db_name) as conn:
                rows = conn.execute(
                    text(
                        "SELECT grantee, privilege_type FROM information_schema.table_privileges "
                        "WHERE table_schema = 'public'"
                    )
                ).fetchall()
                for grantee, priv in rows:
                    if priv != "USAGE":
                        _add(grantee, priv, "table")
        except SQLAlchemyError:
            pass  # el nivel servidor (datacl) es la señal primaria; esto solo enriquece

        return [
            DatabaseGranteeInfo(
                username=u, host=None, privileges=sorted(e["privs"]),
                levels=sorted(e["levels"]), is_global=False,
            )
            for u, e in agg.items()
        ]

    # ------------- conversión de collation (modo ``columns``, PostgreSQL) ----- #
    # PostgreSQL NO tiene el problema que resuelve el modo ``universal`` de MySQL/MariaDB, y
    # por eso este bloque implementa una operación DISTINTA, no una traducción del otro:
    #
    # 1. NADA de recrear vistas/funciones/triggers: PostgreSQL resuelve la collation
    #    DINÁMICAMENTE en cada ejecución, contra el tipo real de la columna en ESE momento.
    #    Una función plpgsql o una vista no congelan nada al crearse, así que ``objects``
    #    es SIEMPRE ``[]`` y ``capture_object_ddl``/``routine_grants`` nunca se llaman.
    # 2. NADA de ``ALTER DATABASE``: el ``ENCODING``/``LC_COLLATE``/``LC_CTYPE`` de una BD
    #    es INMUTABLE tras el ``CREATE DATABASE`` (cambiarlos exige volcar y recargar; para
    #    eso está el módulo de clonado).
    # 3. La ÚNICA unidad de cambio es la COLUMNA:
    #    ``ALTER TABLE t ALTER COLUMN c TYPE <mismo tipo> COLLATE "x"``.
    supports_collation_conversion = True

    # Whitelist del TEXTO DE TIPO que puede viajar al DDL. El valor sale de
    # ``format_type()`` (catálogo del motor, no del cliente) y ya viene re-parseable, pero
    # se interpola en la sentencia: acotarlo es la última barrera si un tipo definido por el
    # usuario tuviera un nombre exótico. Cubre ``text``, ``character varying(255)``,
    # ``"MiDominio"``, ``public.citext``, ``text[]``. Lo que no matchee se EXCLUYE del
    # inventario con una nota, nunca se emite a ciegas.
    _PG_TYPE_RE = re.compile(r'^[A-Za-z0-9_ ."\[\]().,]{1,200}$')

    # Los nombres de collation son identificadores CASE-SENSITIVE que admiten puntos y
    # guiones (``en_US.utf8``, ``es-ES-x-icu``), así que se validan con la whitelist
    # ``allow_existing`` de ``identifiers``. Pero la barrera REAL es otra: el valor tiene que
    # existir en el catálogo VIVO del servidor (``list_collations``) antes de llegar al DDL.

    def list_collations(self, database: str) -> list[CollationOptionInfo]:
        """
        Collations REALES del servidor usables por ``database`` (``pg_collation``).

        Por qué EN VIVO y no desde el catálogo global del gateway: qué collations existen
        depende de los locales instalados en el SO de ESA máquina (y de si el binario trae
        ICU). Dos servidores PostgreSQL idénticos en versión pueden tener catálogos
        distintos, así que una lista global sería falsa. Y no es el mismo espacio de valores
        que ``charset_collation_options``: ahí viven los ``ENCODING``/``LC_COLLATE`` con los
        que se CREA una base (locales del SO como ``en_US.UTF-8``); acá, nombres de OBJETOS
        de ``pg_collation`` (``en_US``, ``C``, ``es-ES-x-icu``).

        Filtro de compatibilidad, TAL CUAL lo define la doc de ``pg_collation``: "PostgreSQL
        generally ignores all collations that do not have ``collencoding`` equal to either
        the current database's encoding or -1". ``-1`` = sirve para cualquier encoding.
        Ofrecer una incompatible daría ``collation "x" for encoding "UTF8" does not exist``
        al usarla. El filtro va por ``collencoding``, NO por ``collprovider``: que las
        collations ICU sean independientes del encoding es una consecuencia, no la regla
        (``ucs_basic``/``pg_c_utf8`` están fijadas a UTF8 y no son ICU).

        ``default`` se EXCLUYE a propósito: no nombra una collation concreta sino "la de la
        base", así que como OBJETIVO de una conversión no significaría nada (y dejaría la
        columna sin collation explícita, que es justo el estado del que se quiere salir).
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        try:
            with database_connection(self.target, database) as conn:
                return self._collations(conn)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_collations", target=self.target, extra={"database": database}
            )

    def _collations(
        self, conn, notes: list[str] | None = None
    ) -> list[CollationOptionInfo]:
        """
        Lectura de ``pg_collation`` degradando por versión (``collprovider`` es PG 10+ y
        ``collisdeterministic`` PG 12+: si el SELECT completo falla se reintenta con el
        nombre solo, en vez de perder el catálogo entero).

        Las collations viven en SCHEMAS y ``collname`` es único por (namespace, encoding),
        así que un mismo nombre puede existir en más de uno. El ``COLLATE`` que se emite va
        SIN calificar y lo resuelve el ``search_path``: si un nombre está duplicado se
        anota, porque ahí el motor podría aplicar una collation distinta de la que el
        operador creyó elegir.
        """
        rows = self._safe_fetch(
            conn,
            "SELECT c.collname, n.nspname, c.collprovider, c.collisdeterministic "
            "FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace "
            "WHERE (c.collencoding = -1 OR c.collencoding = ("
            "  SELECT encoding FROM pg_database WHERE datname = current_database())) "
            "  AND c.collname <> 'default' "
            "ORDER BY c.collname, n.nspname",
        )
        if not rows:
            rows = [
                (r[0], r[1], None, True)
                for r in self._safe_fetch(
                    conn,
                    "SELECT c.collname, n.nspname FROM pg_collation c "
                    "JOIN pg_namespace n ON n.oid = c.collnamespace "
                    "WHERE c.collname <> 'default' ORDER BY c.collname, n.nspname",
                )
            ]
        seen: dict[str, set[str]] = {}
        out: list[CollationOptionInfo] = []
        for name, schema, provider, deterministic in rows:
            key = str(name)
            if not key:
                continue
            schemas = seen.setdefault(key, set())
            if schemas:  # ya emitida: solo se registra el schema para detectar ambigüedad
                schemas.add(str(schema))
                continue
            schemas.add(str(schema))
            out.append(
                CollationOptionInfo(
                    name=key,
                    provider=str(provider) if provider else None,
                    deterministic=bool(deterministic) if deterministic is not None else True,
                )
            )
        ambiguous = sorted(n for n, s in seen.items() if len(s) > 1)
        if ambiguous and notes is not None:
            sample = ", ".join(ambiguous[:5])
            notes.append(
                f"{len(ambiguous)} nombre(s) de collation existen en más de un schema "
                f"({sample}). El COLLATE se emite sin calificar y lo resuelve el "
                "search_path, así que podría aplicarse una distinta de la esperada."
            )
        return out

    def _collatable_columns(self, conn, notes: list[str]) -> dict[str, list[ColumnCollationInfo]]:
        """
        ``{tabla: [columnas colacionables]}`` del schema ``public``.

        La fuente es ``pg_attribute.attcollation``, no una lista de tipos: esa columna es
        distinta de cero EXACTAMENTE en las columnas que tienen collation (``text``,
        ``varchar``, ``char``, ``citext``, dominios sobre ellos, arrays de texto), así que
        no hay que adivinar qué tipos son colacionables en cada versión ni perder los
        definidos por el usuario. ``format_type`` da el tipo con sus parámetros exactos
        (``character varying(255)``), imprescindible porque el ``ALTER COLUMN ... TYPE``
        debe repetir el MISMO tipo.

        ``attcollation`` apunta a la collation ``default`` (``pg_catalog.default``) cuando
        la columna no declaró ninguna: eso es "hereda la de la base", y se reporta como
        ``current_collation=None`` + ``is_default_collation=True``.
        """
        rows = self._safe_fetch(
            conn,
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
            "       co.collname, nco.nspname "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_collation co ON co.oid = a.attcollation "
            "LEFT JOIN pg_namespace nco ON nco.oid = co.collnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "  AND a.attnum > 0 AND NOT a.attisdropped AND a.attcollation <> 0 "
            "ORDER BY c.relname, a.attnum",
        )
        out: dict[str, list[ColumnCollationInfo]] = {}
        skipped: list[str] = []
        for table, column, data_type, collname, coll_schema in rows:
            type_text = str(data_type or "").strip()
            if not self._PG_TYPE_RE.match(type_text):
                skipped.append(f"{table}.{column}")
                continue
            is_default = bool(
                collname is None
                or (str(collname) == "default" and str(coll_schema or "") == "pg_catalog")
            )
            out.setdefault(str(table), []).append(
                ColumnCollationInfo(
                    name=str(column),
                    data_type=type_text,
                    current_collation=None if is_default else str(collname),
                    is_default_collation=is_default,
                )
            )
        if skipped:
            sample = ", ".join(skipped[:5])
            notes.append(
                f"{len(skipped)} columna(s) quedaron FUERA de la conversión porque su tipo "
                f"tiene una forma que el gateway no emite a ciegas en un ALTER COLUMN "
                f"({sample}). Convertilas a mano si hace falta."
            )
        return out

    def collation_inventory(
        self, database: str, *, target_collation: str | None = None
    ) -> CollationInventory:
        """
        Inventario del modo ``columns``: tablas con sus COLUMNAS de texto y la collation de
        cada una, resumen agrupado POR COLLATION DE COLUMNA y el catálogo vivo de
        collations. ``objects`` es siempre ``[]`` (ver el bloque de arriba).

        A diferencia de MySQL/MariaDB, ``charset``/``collation`` de la tabla van en ``None``:
        PostgreSQL no tiene charset por tabla y la collation es atributo de COLUMNA. El
        default de la BD (``db_charset`` = encoding, ``db_collation`` = ``LC_COLLATE``) se
        informa solo como CONTEXTO: es inmutable y esta operación no lo toca.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        notes: list[str] = [
            "PostgreSQL resuelve la collation dinámicamente: las vistas, funciones y "
            "triggers NO congelan la collation de la sesión que los creó, así que no hay "
            "objetos que recrear (a diferencia de MySQL/MariaDB).",
            "El ENCODING y el LC_COLLATE de la base son INMUTABLES tras el CREATE DATABASE: "
            "esta operación NO los cambia, solo la collation de las columnas indicadas.",
            "Alcance: solo el schema 'public' (misma limitación que el diff de esquema y el "
            "clonado).",
        ]
        target = (target_collation or "").strip() or None
        try:
            with database_connection(self.target, database) as conn:
                defaults = self._database_defaults(conn, database, "public")
                collations = self._collations(conn, notes)
                by_table = self._collatable_columns(conn, notes)
                table_rows = self._safe_fetch(
                    conn,
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                    "ORDER BY c.relname",
                )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="collation_inventory", target=self.target,
                extra={"database": database},
            )

        # Misma exclusión que los otros caminos que enumeran tablas: la contabilidad interna
        # del gateway (``_gw_v_*``/``_gw_stg_*``) no es esquema del usuario.
        names = exclude_gateway_internal_tables(str(r[0]) for r in table_rows)

        tables: list[TableCollationInfo] = []
        for name in names:
            cols = by_table.get(name, [])
            bad = sum(1 for c in cols if self._column_needs_collation(c, target))
            tables.append(
                TableCollationInfo(
                    name=name,
                    charset=None,
                    collation=None,
                    mismatched_columns=bad,
                    # Sin objetivo (llamada informativa) no se puede afirmar nada: una tabla
                    # con columnas de texto queda como "a revisar" y una sin ellas, no.
                    needs_conversion=(bad > 0) if target else bool(cols),
                    columns=cols,
                )
            )

        return CollationInventory(
            database=database,
            engine=self.dialect,
            db_charset=defaults.get("db_charset"),
            db_collation=defaults.get("db_collation"),
            target_charset=None,
            target_collation=target_collation,
            tables=tables,
            summary=self._collation_summary(tables),
            objects=[],
            notes=notes,
            available_collations=collations,
        )

    def columns_to_convert(
        self, table: TableCollationInfo, collation: str
    ) -> list[ColumnCollationInfo]:
        """Columnas de ``table`` que todavía NO están en ``collation`` (las que se alteran)."""
        return [
            c for c in (table.columns or []) if self._column_needs_collation(c, collation)
        ]

    @staticmethod
    def _column_needs_collation(col: ColumnCollationInfo, target: str | None) -> bool:
        """
        ¿Esta columna hay que convertirla al objetivo?

        Una columna SIN ``COLLATE`` explícito (hereda el default de la base) SIEMPRE cuenta
        como pendiente aunque el locale de la base coincida con el objetivo: son dos
        collations distintas para el motor (``pg_catalog.default`` vs. la concreta) y
        comparar una columna con default contra otra con collation explícita distinta es
        justamente lo que dispara el conflicto de collation en tiempo de consulta.
        """
        if target is None:
            return False
        if col.is_default_collation:
            return True
        return col.current_collation != target

    @staticmethod
    def _collation_summary(tables: list[TableCollationInfo]) -> list[CollationGroup]:
        """
        Resumen agrupado por collation de COLUMNA: cuántas columnas y en cuántas tablas.

        En el modo ``universal`` el agrupamiento es por tabla porque ahí la collation es un
        atributo de la tabla; acá lo es de la columna, así que agrupar por tabla no
        respondería la pregunta ("cuántas collations distintas tengo dando vueltas").
        """
        cols: dict[str | None, int] = {}
        tabs: dict[str | None, set[str]] = {}
        for t in tables:
            for c in t.columns or []:
                key = None if c.is_default_collation else c.current_collation
                cols[key] = cols.get(key, 0) + 1
                tabs.setdefault(key, set()).add(t.name)
        return [
            CollationGroup(
                charset=None, collation=key, table_count=len(tabs[key]), column_count=n
            )
            for key, n in sorted(cols.items(), key=lambda kv: (-kv[1], str(kv[0] or "")))
        ]

    def collatable_foreign_keys(self, database: str) -> list[CollatableForeignKey]:
        """
        FKs internas entre columnas COLACIONABLES (schema ``public``).

        Sirve para advertir de una conversión PARCIAL con la semántica correcta de
        PostgreSQL: acá el motor NO rechaza el DDL (a diferencia de MySQL/MariaDB, que
        exigen la misma collation en ambos lados de la FK y fallan con 3780/1832). Las dos
        columnas conviven con collations distintas y el problema aparece al CONSULTAR, en
        el join que la FK justamente promueve.

        Best-effort: si la consulta de catálogo falla, devuelve ``[]`` — un aviso no puede
        tumbar el preview.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        try:
            with database_connection(self.target, database) as conn:
                rows = self._safe_fetch(
                    conn,
                    "SELECT con.conname, src.relname, sa.attname, tgt.relname, ta.attname "
                    "FROM pg_constraint con "
                    "JOIN pg_class src ON src.oid = con.conrelid "
                    "JOIN pg_class tgt ON tgt.oid = con.confrelid "
                    "JOIN pg_namespace n ON n.oid = src.relnamespace "
                    # Se emparejan conkey/confkey posición a posición. Se usa la forma con
                    # DOS unnest en el SELECT de un LATERAL (y no ``unnest(a, b)``, que solo
                    # se admite en el nivel superior del FROM) para que funcione igual en
                    # todas las versiones soportadas.
                    "JOIN LATERAL (SELECT unnest(con.conkey) AS src_att, "
                    "                     unnest(con.confkey) AS tgt_att) AS k ON true "
                    "JOIN pg_attribute sa ON sa.attrelid = con.conrelid "
                    "     AND sa.attnum = k.src_att "
                    "JOIN pg_attribute ta ON ta.attrelid = con.confrelid "
                    "     AND ta.attnum = k.tgt_att "
                    "WHERE con.contype = 'f' AND n.nspname = 'public' "
                    "  AND sa.attcollation <> 0 "
                    "ORDER BY src.relname, sa.attname",
                )
        except SQLAlchemyError:
            return []
        return [
            CollatableForeignKey(
                constraint=str(name) if name else None,
                table=str(table),
                column=str(column),
                referenced_table=str(ref_table),
                referenced_column=str(ref_column),
            )
            for name, table, column, ref_table, ref_column in rows
        ]

    def render_collation_change(
        self, database: str, table: str, columns: list[ColumnCollationInfo], collation: str
    ) -> str:
        """
        UNA sentencia ``ALTER TABLE`` con todas las columnas de la tabla que cambian.

        POR QUÉ UNA SOLA y no una por columna: PostgreSQL admite varias acciones en el mismo
        ``ALTER TABLE``, y agruparlas hace UNA sola pasada sobre la tabla (un solo
        ACCESS EXCLUSIVE lock, una sola reconstrucción de índices) en lugar de N. Además la
        deja ATÓMICA: PostgreSQL tiene DDL transaccional, así que si una columna falla la
        tabla entera queda intacta — no existe el estado "media tabla convertida" que sí
        hay que temer en MySQL/MariaDB.

        NO se emite ``USING``: el tipo destino es EXACTAMENTE el de origen (solo cambia la
        cláusula ``COLLATE``), así que la conversión implícita es la identidad. Repetir el
        tipo con sus parámetros es obligatorio: la gramática de PostgreSQL no tiene un
        ``ALTER COLUMN ... SET COLLATE``; cambiar la collation de una columna SOLO se puede
        expresar como un ``SET DATA TYPE``.

        El ``collation`` ya fue validado contra el catálogo VIVO del servidor por el
        controller; acá se revalida como identificador y se quotea (defensa en profundidad).
        Va SIEMPRE entre comillas dobles: en PostgreSQL los nombres de collation son
        case-sensitive y sin comillas ``en_US`` se plegaría a minúsculas.
        """
        tbl = quote_identifier(
            validate_identifier(table, self.dialect, "tabla", allow_existing=True), self.dialect
        )
        coll = quote_identifier(
            validate_identifier(collation, self.dialect, "collation", allow_existing=True),
            self.dialect,
        )
        actions: list[str] = []
        for col in columns:
            type_text = (col.data_type or "").strip()
            if not self._PG_TYPE_RE.match(type_text):
                raise AppHttpException(
                    message="El tipo de la columna no se puede emitir de forma segura.",
                    status_code=422,
                    context={"table": table, "column": col.name},
                )
            name = quote_identifier(
                validate_identifier(col.name, self.dialect, "columna", allow_existing=True),
                self.dialect,
            )
            actions.append(f"ALTER COLUMN {name} SET DATA TYPE {type_text} COLLATE {coll}")
        return f'ALTER TABLE "public".{tbl} ' + ", ".join(actions)

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

    def _estimate_rows(self, conn, table: str, schema: str) -> int | None:
        # ``reltuples = -1`` significa "nunca se analizó" (PG10+), no "cero filas": es el
        # estado normal de una BD recién restaurada o recién clonada. None => desconocido.
        row = conn.execute(
            text(
                "SELECT c.reltuples::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = :t AND n.nspname = :s"
            ),
            {"t": table, "s": schema},
        ).scalar()
        if row is None or int(row) < 0:
            return None
        return int(row)

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
    # ``DEFAULT nextval('x_seq'::regclass)`` = columna ``serial``. La secuencia que respalda
    # un serial está POSEÍDA por la columna (``pg_depend.deptype='a'``) y el snapshot la
    # excluye A PROPÓSITO (es un detalle de implementación del serial, no un objeto
    # independiente). Pero emitir el default tal cual referencia una secuencia que NUNCA se
    # crea -> ``relation "x_seq" does not exist`` en el primer CREATE TABLE. Reproducir la
    # columna como ``SERIAL`` crea la secuencia, la asocia y fija el default en un solo
    # paso: es exactamente lo que era en el origen.
    _NEXTVAL_RE = re.compile(r"^\s*nextval\s*\(", re.IGNORECASE)
    _SERIAL_BY_TYPE = {
        "smallint": "SMALLSERIAL", "int2": "SMALLSERIAL",
        "integer": "SERIAL", "int": "SERIAL", "int4": "SERIAL",
        "bigint": "BIGSERIAL", "int8": "BIGSERIAL",
    }

    def _serial_type(self, col) -> str | None:
        """``SERIAL``/``BIGSERIAL``/``SMALLSERIAL`` si la columna es un serial, o None.

        Solo se aplica a columnas NOT NULL: ``serial`` de PostgreSQL implica NOT NULL por
        definición, así que usarlo en una columna nullable cambiaría la nullabilidad en
        silencio. Una columna NULLABLE con default ``nextval`` (creada a mano, muy inusual)
        queda con el default crudo — limitación conocida y acotada.
        """
        if col.default is None or col.nullable or col.identity is not None:
            return None
        if not self._NEXTVAL_RE.match(str(col.default)):
            return None
        return self._SERIAL_BY_TYPE.get(str(col.type).strip().lower())

    def _render_column_def(self, col) -> str:
        serial = self._serial_type(col)
        if serial:
            # SERIAL ya aporta tipo + NOT NULL + DEFAULT nextval + la secuencia asociada.
            return f"{self._q(col.name, 'columna')} {serial}"
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

    # ------------------------- ON UPDATE CURRENT_TIMESTAMP (cross-engine) ----- #
    # PostgreSQL no tiene una cláusula de columna equivalente a ``ON UPDATE
    # CURRENT_TIMESTAMP`` de MySQL/MariaDB (``col.on_update``, ver
    # ``MySQLAdapter._render_column_def``) — se implementa con un TRIGGER. Sin esto,
    # una columna que en MySQL se autoactualiza en cada UPDATE deja de hacerlo en
    # silencio al clonar/sincronizar MySQL/MariaDB → PostgreSQL. Postgres NUNCA setea
    # ``on_update`` en su propia introspección (``_column_extras`` de este adapter
    # siempre lo deja en ``None``), así que esto solo se activa en el sentido
    # cross-engine correcto.
    def _render_on_update_trigger_statements(self, item, table: str, col) -> list:
        if not getattr(col, "on_update", None):
            return []
        # Nombre DETERMINISTA (hash de tabla+columna, no aleatorio): idempotente entre
        # corridas — un re-clon/re-sync no acumula funciones/triggers duplicados.
        suffix = hashlib.sha256(f"{table}.{col.name}".encode()).hexdigest()[:16]
        fn_name = self._q(f"gw_ou_{suffix}", "función")
        trg_name = self._q(f"gw_ou_{suffix}", "trigger")
        table_q = self._q(table, "tabla")
        col_q = self._q(col.name, "columna")
        fn_sql = (
            f"CREATE OR REPLACE FUNCTION {fn_name}() RETURNS TRIGGER "
            f"LANGUAGE plpgsql AS $$ BEGIN NEW.{col_q} := CURRENT_TIMESTAMP; "
            f"RETURN NEW; END; $$"
        )
        # DROP + CREATE (en vez de ``CREATE OR REPLACE TRIGGER``, solo disponible desde
        # PostgreSQL 14) para no asumir una versión mínima del motor destino.
        drop_trg_sql = f"DROP TRIGGER IF EXISTS {trg_name} ON {table_q}"
        create_trg_sql = (
            f"CREATE TRIGGER {trg_name} BEFORE UPDATE ON {table_q} "
            f"FOR EACH ROW EXECUTE FUNCTION {fn_name}()"
        )
        return [self._stmt(item, fn_sql), self._stmt(item, drop_trg_sql), self._stmt(item, create_trg_sql)]

    def _on_update_reverse(self, table: str, col) -> list[str]:
        """Reverso de ``_render_on_update_trigger_statements``: suelta trigger y función.

        Sin esto, revertir la columna/tabla dejaba la función ``gw_ou_*`` huérfana en la
        BD (el ``DROP TABLE`` arrastra el trigger, pero nunca la función).
        """
        if not getattr(col, "on_update", None):
            return []
        suffix = hashlib.sha256(f"{table}.{col.name}".encode()).hexdigest()[:16]
        return [
            f"DROP TRIGGER IF EXISTS {self._q(f'gw_ou_{suffix}', 'trigger')} "
            f"ON {self._q(table, 'tabla')}",
            f"DROP FUNCTION IF EXISTS {self._q(f'gw_ou_{suffix}', 'función')}()",
        ]

    @staticmethod
    def _move_reverse_to_last(out: list, extra_reverse_first: list[str]) -> list:
        """
        Reubica el ``down_sql`` del grupo en su ÚLTIMA sentencia, precedido por
        ``extra_reverse_first``.

        Los overrides de PostgreSQL AGREGAN sentencias después de las del renderer base
        (que ya adjuntó su reverso a lo que entonces era la última). ``_stmts`` garantiza
        que el reverso viva en la última sentencia del grupo: se restablece ese invariante.
        """
        if not out:
            return out
        carried = next((s.down_sql for s in out if s.down_sql), None)
        confirmed = next((s.down_confirmed for s in out if s.down_sql), False)
        for s in out:
            s.down_sql, s.down_confirmed = None, False
        parts = [p for p in [*extra_reverse_first, carried] if p]
        if parts:
            out[-1].down_sql = ";\n".join(parts)
            out[-1].down_confirmed = confirmed and not extra_reverse_first
        return out

    def _ri_table_new(self, item):
        out = super()._ri_table_new(item)
        tbl = item.source_payload
        extra: list[str] = []
        for col in tbl.columns:
            stmts = self._render_on_update_trigger_statements(item, tbl.table, col)
            if stmts:
                out.extend(stmts)
                # El DROP TABLE del reverso arrastra el trigger; la función hay que
                # soltarla DESPUÉS de la tabla (mientras el trigger la use, no se puede).
                extra.extend(self._on_update_reverse(tbl.table, col)[1:])
        if not extra:
            return out
        carried = next((s.down_sql for s in out if s.down_sql), None)
        for s in out:
            s.down_sql, s.down_confirmed = None, False
        out[-1].down_sql = ";\n".join([p for p in [carried, *extra] if p])
        out[-1].down_confirmed = False
        return out

    def _ri_column_new(self, item):
        out = super()._ri_column_new(item)
        stmts = self._render_on_update_trigger_statements(
            item, item.parent_table, item.source_payload
        )
        out.extend(stmts)
        return self._move_reverse_to_last(
            out, self._on_update_reverse(item.parent_table, item.source_payload)
        )

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
