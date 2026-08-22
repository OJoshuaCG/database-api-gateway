"""
Adaptador para MySQL y MariaDB.

Particularidades:
- Los usuarios se identifican por el par `'usuario'@'host'`.
- Los permisos se otorgan a nivel de BD entera con `ON `db`.*`.
- No existe "owner" nativo de schema: la propiedad es un concepto lógico que el
  gateway mantiene en su BD de metadatos, respaldado por `GRANT ALL ON db.*`.
"""

import re
from typing import ClassVar

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
    CollationGroup,
    CollationInventory,
    CollationObjectInfo,
    DatabaseGranteeInfo,
    DumpStatement,
    EngineUserInfo,
    EventInfo,
    ExternalFkDependent,
    GrantInfo,
    GrantLevel,
    ObjectRef,
    RoutineGrantInfo,
    RoutineInfo,
    RoutineParam,
    StructureDump,
    TableCollationInfo,
    TriggerInfo,
    ViewInfo,
)
from app.services.db_admin.identifiers import (
    exclude_gateway_internal_tables,
    quote_identifier,
    quote_string_literal,
    validate_host,
    validate_identifier,
    validate_privileges,
)
from app.services.db_admin.sql_dialect import mask_quoted_spans

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

    def export_supported_types(self) -> frozenset[str]:
        """
        El núcleo común + ``event`` (el scheduler de MySQL/MariaDB).

        Lo que NO está tampoco es un olvido: en esta familia no existen las vistas
        materializadas, ni las secuencias autónomas de PostgreSQL, ni los tipos ENUM de
        catálogo (el ``ENUM`` de MySQL es un tipo de COLUMNA, no un objeto), ni las
        extensiones.
        """
        return super().export_supported_types() | {"event"}

    # ---- Emisión del artefacto de exportación (§7) ----------------------- #
    # Variables de sesión que el preámbulo cambia y el epílogo RESTAURA. Se guardan en
    # variables de usuario con prefijo ``@_gw_`` (mismo patrón que ``_gw_v_``/``_gw_stg_``)
    # en vez de fijar valores "por defecto" al salir: restaurar a un valor supuesto pisaría
    # la configuración de quien ejecuta el script. Es el mismo mecanismo de ``mysqldump``.
    # ``CREATE TABLE/EVENT IF NOT EXISTS`` existen desde siempre. Las rutinas y los triggers
    # NO están: su ``IF NOT EXISTS`` es de MySQL 8.0.29+ / MariaDB 10.1+ y el gateway no
    # conoce la versión del destino donde se ejecutará el artefacto.
    _EXPORT_IF_NOT_EXISTS_TYPES = frozenset({"table", "event"})
    # Las vistas se rendean con ``CREATE OR REPLACE VIEW``: ya son idempotentes.
    _EXPORT_ALREADY_IDEMPOTENT_TYPES = frozenset({"view"})

    _EXPORT_SESSION_VARS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("_gw_fk_checks", "FOREIGN_KEY_CHECKS"),
        ("_gw_unique_checks", "UNIQUE_CHECKS"),
        ("_gw_sql_mode", "SQL_MODE"),
        ("_gw_time_zone", "TIME_ZONE"),
    )

    def export_scope_ddl(
        self, database, mode, *, charset=None, collation=None, if_exists=True
    ) -> list[str]:
        mode = str(mode)
        if mode == "NONE":
            return []
        db = self._q(database, "base de datos")
        out: list[str] = []
        if mode == "DROP_CREATE":
            out.append(f"DROP DATABASE {'IF EXISTS ' if if_exists else ''}{db}")
        create = "CREATE DATABASE"
        if mode == "CREATE_IF_NOT_EXISTS":
            create += " IF NOT EXISTS"
        sql = f"{create} {db}"
        if charset:
            cs = validate_identifier(charset, self.dialect, "charset", allow_existing=True)
            sql += f" DEFAULT CHARACTER SET {cs}"
        if collation:
            co = validate_identifier(
                collation, self.dialect, "collation", allow_existing=True
            )
            sql += f" DEFAULT COLLATE {co}"
        out.append(sql)
        return out

    def export_session_preamble(
        self, *, charset=None, collation=None, suspend_constraints=True
    ) -> list[str]:
        """
        Guarda el estado de la sesión, lo relaja para cargar datos y fija el juego de
        caracteres del script. ``SET NAMES`` va SIEMPRE: sin él, el cliente interpreta el
        archivo con su charset por defecto y los literales multibyte se corrompen en
        silencio, que es el peor fallo posible (no aborta nada, solo escribe mal).
        """
        out = [
            f"SET @{var} = @@{sysvar}" for var, sysvar in self._EXPORT_SESSION_VARS
        ]
        cs = validate_identifier(
            charset or "utf8mb4", self.dialect, "charset", allow_existing=True
        )
        out.append(f"SET NAMES {cs}")
        if suspend_constraints:
            out.append("SET FOREIGN_KEY_CHECKS = 0")
            out.append("SET UNIQUE_CHECKS = 0")
        # ``NO_AUTO_VALUE_ON_ZERO``: sin esto un 0 explícito en una columna AUTO_INCREMENT
        # se convierte en "el siguiente valor" y el volcado deja de reproducir el origen.
        out.append("SET SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO'")
        # UTC: un TIMESTAMP se guarda en UTC y se muestra en la zona de la SESIÓN, así que
        # sin fijarla el mismo artefacto restaurado en otra zona corre los valores.
        out.append("SET TIME_ZONE = '+00:00'")
        return out

    def export_session_epilogue(self) -> list[str]:
        return [f"SET {sysvar} = @{var}" for var, sysvar in self._EXPORT_SESSION_VARS]

    def export_use_scope(self, database: str) -> str | None:
        return f"USE {self._q(database, 'base de datos')}"

    def export_counter_reset(
        self, table: str, value: int | None, *, column: str | None = None
    ) -> str | None:
        # ``column`` no se usa: en MySQL/MariaDB el contador es una opción de la TABLA.
        if value is None:
            return None
        return f"ALTER TABLE {self._q(table, 'tabla')} AUTO_INCREMENT = {int(value)}"

    def export_counter_value_sql(
        self, database: str, table: str, column: str
    ) -> tuple[str, dict] | None:
        """
        El contador es una propiedad de la TABLA y ``information_schema`` ya publica el
        PRÓXIMO valor que el motor va a usar — que es exactamente lo que espera
        ``ALTER TABLE … AUTO_INCREMENT = n``.

        Se lee de ahí y no con un ``MAX(col)+1``: el máximo recorre la tabla entera (caro y
        además dentro de la transacción de consistencia del job) y no coincide con el
        contador cuando hubo borrados o huecos por transacciones abortadas.
        """
        return (
            "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl",
            {"db": database, "tbl": table},
        )

    def export_insert_wrapper(
        self, table, columns, *, variant="insert", primary_key=()
    ) -> tuple[str, str]:
        q = self._q(table, "tabla")
        cols = self._export_column_list(columns)
        if variant in ("insert", "none"):
            return f"INSERT INTO {q}{cols} VALUES", ""
        if variant == "insert_ignore":
            return f"INSERT IGNORE INTO {q}{cols} VALUES", ""
        if variant == "replace":
            # ``REPLACE`` es DELETE+INSERT, no un UPDATE: dispara los ON DELETE de las FKs y
            # cambia el autoincrement de la fila. Es lo que el usuario pidió, pero no es
            # equivalente a ``upsert`` y por eso son opciones distintas.
            return f"REPLACE INTO {q}{cols} VALUES", ""
        if variant == "upsert":
            updatable = [c for c in columns if c not in set(primary_key)]
            if not columns or not updatable:
                # Sin lista de columnas no hay nada que nombrar en el SET, y si todas las
                # columnas son la PK el upsert no tiene qué actualizar: en vez de emitir una
                # sentencia inválida se devuelve un 422 accionable.
                raise self._export_unsupported_variant("upsert")
            # ``VALUES(col)`` y no el alias de fila de MySQL 8.0.20+: MariaDB no soporta el
            # alias, y el artefacto tiene que poder ejecutarse en ambos.
            assignments = ", ".join(
                f"{self._q(c, 'columna')} = VALUES({self._q(c, 'columna')})"
                for c in updatable
            )
            return f"INSERT INTO {q}{cols} VALUES", f" ON DUPLICATE KEY UPDATE {assignments}"
        raise self._export_unsupported_variant(variant)

    def export_definer_clause(self, sql: str, *, mode: str, value=None) -> str:
        """
        ``keep`` deja el DDL como está; ``omit`` quita la cláusula; ``replace`` la fija.

        AVISO sobre ``keep``: los cuerpos del ``SchemaSnapshot`` ya vienen SIN ``DEFINER``
        (``_strip_definer_clause`` corre al capturarlos, porque re-aplicarlos en otro
        servidor con un usuario inexistente falla). Por eso ``keep`` es, en la práctica,
        indistinguible de ``omit`` en el camino del snapshot: no hay nada que conservar. Se
        implementa igual para que el día que exista una fuente que sí lo traiga (un
        ``SHOW CREATE`` sin sanear) el comportamiento sea el correcto.

        SEGURIDAD: ``definer_value`` es entrada del cliente que termina DENTRO del DDL.
        No se interpola: se parte en usuario/host, se valida cada parte y se re-emite
        delimitada con backticks.
        """
        mode = str(mode)
        if mode == "keep":
            return sql
        stripped = self._strip_definer_clause(sql)
        if mode != "replace":
            return stripped
        clause = self._export_definer_literal(value)
        # La cláusula va inmediatamente después de ``CREATE`` (y de ``OR REPLACE`` si
        # está), que es la única posición que MySQL/MariaDB aceptan.
        match = re.match(r"(\s*CREATE\s+(?:OR\s+REPLACE\s+)?)", stripped, re.IGNORECASE)
        if not match:
            # DDL que no empieza con CREATE: no hay dónde poner el DEFINER. Se deja sin
            # cláusula en vez de inyectarla en un lugar arbitrario (fail-closed).
            return stripped
        head = match.group(1)
        return f"{head}{clause} {stripped[len(head):]}"

    def _export_definer_literal(self, value: str | None) -> str:
        """``'app'@'%'`` / ``app@%`` / ``CURRENT_USER`` → ``DEFINER=`app`@`%``` validado."""
        raw = (value or "").strip()
        if not raw:
            raise AppHttpException(
                message="Falta el valor del DEFINER de reemplazo.",
                status_code=422,
                context={"field": "sanitize.definer_value"},
            )
        if raw.upper() in ("CURRENT_USER", "CURRENT_USER()"):
            return "DEFINER=CURRENT_USER"
        user, sep, host = raw.rpartition("@")
        if not sep:
            user, host = raw, "%"
        user = user.strip().strip("`'\"")
        host = host.strip().strip("`'\"") or "%"
        validate_identifier(user, self.dialect, "usuario", allow_existing=True)
        validate_host(host)
        return (
            f"DEFINER={quote_identifier(user, self.dialect)}@"
            f"{quote_identifier(host, self.dialect)}"
        )

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

    def external_fk_dependents(self, database: str) -> list[ExternalFkDependent]:
        # information_schema.KEY_COLUMN_USAGE es a nivel de SERVIDOR (no de una sola BD):
        # detecta columnas en CUALQUIER otra BD del servidor cuya FK referencia una tabla
        # de `database`. REFERENCED_TABLE_NAME IS NOT NULL filtra solo entradas de FK (no
        # PK/UNIQUE, que también aparecen en esta vista sin referenced_table).
        sql = (
            "SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS table_name, "
            "COLUMN_NAME AS column_name, CONSTRAINT_NAME AS constraint_name, "
            "REFERENCED_TABLE_NAME AS referenced_table, "
            "REFERENCED_COLUMN_NAME AS referenced_column "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE REFERENCED_TABLE_SCHEMA = :db AND TABLE_SCHEMA <> :db "
            "AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(sql), {"db": database}).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="external_fk_dependents", target=self.target)
        return [
            ExternalFkDependent(
                schema_name=r.schema_name, table=r.table_name, column=r.column_name,
                constraint=r.constraint_name, referenced_table=r.referenced_table,
                referenced_column=r.referenced_column,
            )
            for r in rows
        ]

    # ------------------------- snapshot estructural (Plan 09) ----------------- #
    @staticmethod
    def _show_create_value(row, candidates: tuple[str, ...], fallback_idx: int) -> str:
        """Extrae el DDL de una fila de SHOW CREATE por nombre de columna (o índice)."""
        mapping = row._mapping
        for key in candidates:
            if key in mapping:
                return mapping[key]
        return row[fallback_idx]

    def dump_structure(self, database: str, *, conn=None) -> StructureDump:
        """
        Dump estructural de una BD MySQL/MariaDB vía ``SHOW CREATE *``.

        Orden de dependencia: tablas → vistas → rutinas → triggers → events. El
        ``DEFINER`` se sanea (ver base). Solo estructura, nunca filas.

        ``conn`` (§6.4 del módulo de exportación): ver ``ServerAdapter._conn_ctx``.
        ``None`` = comportamiento histórico (conexión propia).
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        statements: list[DumpStatement] = []
        has_non_portable = False
        try:
            with self._conn_ctx(database, conn) as conn:
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

                # 1) Tablas base (no vistas). Se excluye la contabilidad interna del
                #    gateway (``_gw_v_*``/``_gw_stg_*``): un baseline de snapshot que la
                #    incluyera emitiría ``CREATE TABLE _gw_v_{slug}`` en su versión 0001,
                #    inyectando una tabla de versión ajena en cada BD donde se aplique.
                tables = exclude_gateway_internal_tables(
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT TABLE_NAME FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE' "
                            "ORDER BY TABLE_NAME"
                        ),
                        {"db": database},
                    ).fetchall()
                )
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

    def drop_database(self, db_name, *, force_disconnect=False) -> None:
        # ``allow_existing``: la BD a borrar puede tener un nombre legado (dígito inicial,
        # ``.-$``) que la whitelist estricta rechaza. ``force_disconnect`` es NO-OP en
        # MySQL/MariaDB: el motor no bloquea el DROP por conexiones activas (a diferencia
        # de PostgreSQL); se acepta el kwarg solo por paridad de contrato.
        validate_identifier(db_name, self.dialect, "base de datos", allow_existing=True)
        db = quote_identifier(db_name, self.dialect)
        self._execute_server(
            [f"DROP DATABASE {db}"], op="drop_database", extra={"database": db_name}
        )

    def active_connections(self, db_name) -> int:
        validate_identifier(db_name, self.dialect, "base de datos", allow_existing=True)
        sql = "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE DB = :db"
        try:
            with server_connection(self.target) as conn:
                return int(conn.execute(text(sql), {"db": db_name}).scalar() or 0)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="active_connections", target=self.target, extra={"database": db_name}
            )

    # Consulta INVERSA (por BD, agrupada por GRANTEE). Los privilegios globales ``*.*``
    # (USER_PRIVILEGES) se incluyen SIEMPRE: aplican a TODAS las BDs, así que un usuario con
    # ``GRANT SELECT ON *.*`` tiene acceso efectivo a ESTA BD aunque no tenga grant directo.
    _LIST_DB_GRANTEES_SQL = (
        "SELECT 'global' AS lvl, GRANTEE AS grantee, PRIVILEGE_TYPE AS p "
        "  FROM information_schema.USER_PRIVILEGES "
        "UNION ALL SELECT 'database', GRANTEE, PRIVILEGE_TYPE "
        "  FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = :db "
        "UNION ALL SELECT 'table', GRANTEE, PRIVILEGE_TYPE "
        "  FROM information_schema.TABLE_PRIVILEGES WHERE TABLE_SCHEMA = :db "
        "UNION ALL SELECT 'column', GRANTEE, PRIVILEGE_TYPE "
        "  FROM information_schema.COLUMN_PRIVILEGES WHERE TABLE_SCHEMA = :db"
    )

    @staticmethod
    def _parse_grantee(grantee: str) -> tuple[str, str]:
        """``information_schema`` reporta el grantee como ``'user'@'host'``."""
        raw = grantee or ""
        if "@" in raw:
            user, host = raw.rsplit("@", 1)
        else:
            user, host = raw, "%"
        return user.strip().strip("'"), host.strip().strip("'")

    def list_database_grantees(self, db_name) -> list[DatabaseGranteeInfo]:
        validate_identifier(db_name, self.dialect, "base de datos", allow_existing=True)
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(
                    text(self._LIST_DB_GRANTEES_SQL), {"db": db_name}
                ).fetchall()
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_database_grantees", target=self.target,
                extra={"database": db_name},
            )
        agg: dict[tuple[str, str], dict] = {}
        for lvl, grantee, priv in rows:
            if priv == "USAGE":
                continue  # "sin privilegios": no denota relación real
            username, host = self._parse_grantee(grantee)
            if username in _SYSTEM_USERS or not username:
                continue
            entry = agg.setdefault(
                (username, host), {"privs": set(), "levels": set(), "is_global": False}
            )
            entry["privs"].add(priv)
            entry["levels"].add(lvl)
            if lvl == "global":
                entry["is_global"] = True
        return [
            DatabaseGranteeInfo(
                username=u, host=h, privileges=sorted(e["privs"]),
                levels=sorted(e["levels"]), is_global=e["is_global"],
            )
            for (u, h), e in agg.items()
            if e["privs"]
        ]

    # ------------- conversión de charset/collation (universal MySQL/MariaDB) -- #
    # MySQL/MariaDB son los ÚNICOS motores con el problema que este bloque resuelve, y la
    # documentación oficial de MySQL lo dice sin ambigüedad (ALTER DATABASE):
    #   "If you change the default character set or collation for a database, any stored
    #    routines that are to use the new defaults must be dropped and recreated."
    # El motor CONGELA en cada PROCEDURE/FUNCTION/TRIGGER/EVENT/VIEW la ``collation_connection``
    # de la sesión que lo creó (es la que heredan los parámetros VARCHAR/CHAR de una rutina,
    # las variables DECLARE de un trigger/evento y los literales del cuerpo de una vista).
    # No existe ALTER que cambie eso: la única vía es DROP + CREATE con el MISMO cuerpo.
    supports_collation_conversion = True

    # (object_type, tabla de information_schema, columna del nombre, columna del schema,
    #  ¿expone DATABASE_COLLATION?). VERIFICADO contra la doc oficial de MySQL 8.0/8.4 y
    # MariaDB 10.x/11.x: ROUTINES/TRIGGERS/EVENTS traen las TRES columnas, pero
    # ``information_schema.VIEWS`` NO tiene ``DATABASE_COLLATION`` en NINGUNO de los dos
    # motores (su lista documentada son 10 columnas en MySQL; MariaDB agrega ALGORITHM, no
    # DATABASE_COLLATION). Pedirla en el SELECT de VIEWS haría fallar la consulta entera.
    _FROZEN_OBJECT_SOURCES: tuple[tuple[str, str, str, str, bool], ...] = (
        # ROUTINES cubre procedure y function: el object_type real sale de ROUTINE_TYPE.
        ("routine", "information_schema.ROUTINES", "ROUTINE_NAME", "ROUTINE_SCHEMA", True),
        ("trigger", "information_schema.TRIGGERS", "TRIGGER_NAME", "TRIGGER_SCHEMA", True),
        ("event", "information_schema.EVENTS", "EVENT_NAME", "EVENT_SCHEMA", True),
        ("view", "information_schema.VIEWS", "TABLE_NAME", "TABLE_SCHEMA", False),
    )

    # object_type → (palabra de SHOW CREATE, candidatos de nombre de columna, índice fallback).
    # Los índices/nombres replican los que ya usa ``dump_structure``/``_snapshot_*``.
    _SHOW_CREATE_SPECS: ClassVar[dict[str, tuple[str, tuple[str, ...], int]]] = {
        "procedure": ("PROCEDURE", ("Create Procedure",), 2),
        "function": ("FUNCTION", ("Create Function",), 2),
        "trigger": ("TRIGGER", ("SQL Original Statement",), 2),
        "event": ("EVENT", ("Create Event",), 3),
        "view": ("VIEW", ("Create View",), 1),
    }

    # Privilegios de rutina que el gateway sabe reaplicar. ``mysql.procs_priv.Proc_priv`` es
    # un SET cuyos valores documentados son EXACTAMENTE 'Execute', 'Alter Routine' y 'Grant'
    # (MySQL "Set-Type Privilege Column Values"; MariaDB idéntico). 'Grant' no es un
    # privilegio que se pueda nombrar en un GRANT: se confiere con WITH GRANT OPTION.
    _PROCS_PRIV_MAP: ClassVar[dict[str, str]] = {
        "execute": "EXECUTE",
        "alter routine": "ALTER ROUTINE",
    }

    @staticmethod
    def _charset_of(collation: str | None, cs_by_collation: dict[str, str]) -> str | None:
        """
        Charset de una collation. ``information_schema.TABLES`` NO expone el charset de la
        tabla (la doc de MySQL lo dice explícitamente: "The output does not explicitly list
        the table default character set, but the collation name begins with the character
        set name"), así que se resuelve contra ``information_schema.COLLATIONS`` y, si esa
        consulta no estuvo disponible, se cae al prefijo del nombre.
        """
        if not collation:
            return None
        hit = cs_by_collation.get(collation.lower())
        if hit:
            return hit
        return collation.split("_", 1)[0] or None

    def _collation_charset_map(self, conn) -> dict[str, str]:
        """``{collation_lower: charset}`` desde ``information_schema.COLLATIONS``."""
        try:
            rows = conn.execute(
                text(
                    "SELECT COLLATION_NAME, CHARACTER_SET_NAME "
                    "FROM information_schema.COLLATIONS"
                )
            ).fetchall()
        except SQLAlchemyError:
            return {}
        return {str(c).lower(): str(cs) for c, cs in rows if c and cs}

    def _frozen_objects(
        self, conn, database: str, notes: list[str]
    ) -> list[CollationObjectInfo]:
        """
        Los 5 tipos con collation congelada, con su ``collation_connection``.

        FAIL-CLOSED por FUENTE: cada tabla de ``information_schema`` se consulta por
        separado y, si la consulta falla (columna ausente en una versión que no
        contemplamos, ``EVENTS`` inexistente en una instalación mínima, permisos), se
        reintenta pidiendo SOLO el nombre y se anota el motivo en ``notes``. Así el
        objeto sigue apareciendo en el inventario (y podrá recrearse) aunque se pierda
        la señal de "está desactualizado" — nunca se lo oculta en silencio.
        """
        out: list[CollationObjectInfo] = []
        for otype, source, name_col, schema_col, has_db_coll in self._FROZEN_OBJECT_SOURCES:
            cols = [name_col, "CHARACTER_SET_CLIENT", "COLLATION_CONNECTION"]
            if has_db_coll:
                cols.append("DATABASE_COLLATION")
            if otype == "routine":
                cols.append("ROUTINE_TYPE")
            select_list = ", ".join(cols)
            rows = None
            degraded = False
            try:
                rows = conn.execute(
                    text(
                        f"SELECT {select_list} FROM {source} "
                        f"WHERE {schema_col} = :db ORDER BY {name_col}"
                    ),
                    {"db": database},
                ).fetchall()
            except SQLAlchemyError:
                # Reintento degradado: solo el nombre (+ ROUTINE_TYPE para distinguir
                # procedure de function, sin el cual el objeto no se puede ni recrear).
                minimal = name_col + (", ROUTINE_TYPE" if otype == "routine" else "")
                try:
                    rows = conn.execute(
                        text(
                            f"SELECT {minimal} FROM {source} "
                            f"WHERE {schema_col} = :db ORDER BY {name_col}"
                        ),
                        {"db": database},
                    ).fetchall()
                    degraded = True
                    notes.append(
                        f"No se pudo leer la collation de creación de los objetos de tipo "
                        f"'{otype}' ({source}); se listan sin esa señal y se recrearán igual "
                        f"si se seleccionan."
                    )
                except SQLAlchemyError:
                    notes.append(
                        f"No se pudieron listar los objetos de tipo '{otype}' ({source} no "
                        f"disponible en este servidor); quedan FUERA de la conversión."
                    )
                    continue
            for row in rows or []:
                m = row._mapping
                name = m[name_col]
                if otype == "routine":
                    rtype = str(m.get("ROUTINE_TYPE") or "").upper()
                    real_type = "function" if rtype == "FUNCTION" else "procedure"
                else:
                    real_type = otype
                out.append(
                    CollationObjectInfo(
                        object_type=real_type,
                        name=str(name),
                        character_set_client=(
                            None if degraded else (
                                str(m["CHARACTER_SET_CLIENT"])
                                if m.get("CHARACTER_SET_CLIENT") else None
                            )
                        ),
                        collation_connection=(
                            None if degraded else (
                                str(m["COLLATION_CONNECTION"])
                                if m.get("COLLATION_CONNECTION") else None
                            )
                        ),
                        database_collation=(
                            None if (degraded or not has_db_coll) else (
                                str(m["DATABASE_COLLATION"])
                                if m.get("DATABASE_COLLATION") else None
                            )
                        ),
                    )
                )
        return out

    def collation_inventory(
        self, database: str, *, target_collation: str | None = None
    ) -> CollationInventory:
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        target_norm = (target_collation or "").lower() or None
        notes: list[str] = []
        try:
            with database_connection(self.target, database) as conn:
                cs_by_coll = self._collation_charset_map(conn)

                db_row = conn.execute(
                    text(
                        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                        "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :db"
                    ),
                    {"db": database},
                ).fetchone()
                db_charset = str(db_row[0]) if db_row and db_row[0] else None
                db_collation = str(db_row[1]) if db_row and db_row[1] else None

                # Tablas base. Se EXCLUYE la contabilidad interna del gateway
                # (``_gw_v_*``/``_gw_stg_*``): es el mismo invariante que respetan los otros
                # cuatro caminos que enumeran tablas (ver identifiers.GATEWAY_TABLE_PREFIXES).
                # No es esquema del usuario y no debe aparecer en una selección suya.
                table_rows = conn.execute(
                    text(
                        "SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE' "
                        "ORDER BY TABLE_NAME"
                    ),
                    {"db": database},
                ).fetchall()
                visible = set(exclude_gateway_internal_tables(r[0] for r in table_rows))

                # Collation POR COLUMNA. Imprescindible: una tabla cuyo TABLE_COLLATION ya
                # es el objetivo puede tener columnas con COLLATE explícito distinto, así que
                # decidir "no necesita conversión" mirando solo el default sería incorrecto.
                # ``COLLATION_NAME IS NOT NULL`` deja fuera las columnas no textuales.
                mismatch: dict[str, int] = {}
                if target_norm:
                    try:
                        for tname, coll in conn.execute(
                            text(
                                "SELECT TABLE_NAME, COLLATION_NAME "
                                "FROM information_schema.COLUMNS "
                                "WHERE TABLE_SCHEMA = :db AND COLLATION_NAME IS NOT NULL"
                            ),
                            {"db": database},
                        ).fetchall():
                            if str(coll).lower() != target_norm:
                                mismatch[str(tname)] = mismatch.get(str(tname), 0) + 1
                    except SQLAlchemyError:
                        notes.append(
                            "No se pudo leer la collation por columna; las tablas se "
                            "convertirán sin poder saltear las que ya estaban al día."
                        )

                tables: list[TableCollationInfo] = []
                for name, coll in table_rows:
                    if str(name) not in visible:
                        continue
                    collation = str(coll) if coll else None
                    bad_cols = mismatch.get(str(name), 0)
                    needs = True
                    if target_norm:
                        needs = (
                            collation is None
                            or collation.lower() != target_norm
                            or bad_cols > 0
                        )
                    tables.append(
                        TableCollationInfo(
                            name=str(name),
                            charset=self._charset_of(collation, cs_by_coll),
                            collation=collation,
                            mismatched_columns=bad_cols,
                            needs_conversion=needs,
                        )
                    )

                objects = self._frozen_objects(conn, database, notes)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="collation_inventory", target=self.target,
                extra={"database": database},
            )

        # Resumen agrupado por par (charset, collation): "cuántos collation distintos hay y
        # cuántas tablas en cada uno".
        counts: dict[tuple[str | None, str | None], int] = {}
        for t in tables:
            key = (t.charset, t.collation)
            counts[key] = counts.get(key, 0) + 1
        summary = [
            CollationGroup(charset=cs, collation=co, table_count=n)
            for (cs, co), n in sorted(
                counts.items(), key=lambda kv: (-kv[1], str(kv[0][1] or ""))
            )
        ]

        for obj in objects:
            # Sin señal de collation (lectura degradada) NO se afirma que esté al día:
            # fail-closed → se marca como desactualizado para que entre en la selección.
            if target_norm is None:
                obj.is_outdated = False
            elif obj.collation_connection is None:
                obj.is_outdated = True
            else:
                obj.is_outdated = obj.collation_connection.lower() != target_norm

        return CollationInventory(
            database=database,
            engine=self.dialect,
            db_charset=db_charset,
            db_collation=db_collation,
            target_charset=None,
            target_collation=target_collation,
            tables=tables,
            summary=summary,
            objects=objects,
            notes=notes,
        )

    def capture_object_ddl(self, database: str, object_type: str, name: str) -> str:
        """
        DDL exacto de UN objeto vía ``SHOW CREATE``.

        DIFERENCIA DELIBERADA con ``dump_structure``/el clon: acá el ``DEFINER`` **NO se
        sanea**, se preserva VERBATIM. Aquellos cruzan de servidor, donde un DEFINER que no
        existe en el destino rompe el CREATE; esta operación recrea el objeto en la MISMA BD
        del MISMO servidor, así que el usuario del DEFINER sigue existiendo. Y quitarlo NO
        sería neutro: una rutina/vista ``SQL SECURITY DEFINER`` pasaría a ejecutarse con la
        credencial pseudo-root del gateway — una ESCALADA DE PRIVILEGIOS silenciosa. Si el
        pseudo-root no tiene permiso para fijar un DEFINER ajeno (``SET_USER_ID``/``SUPER``),
        el CREATE falla y el paso se reporta como error, que es el resultado correcto:
        preferible a recrear el objeto con permisos distintos de los que tenía.
        """
        spec = self._SHOW_CREATE_SPECS.get(object_type)
        if spec is None:
            raise AppHttpException(
                message="Tipo de objeto no soportado para captura de DDL.",
                status_code=422,
                context={"object_type": object_type},
            )
        keyword, candidates, fallback_idx = spec
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        q = quote_identifier(
            validate_identifier(name, self.dialect, object_type, allow_existing=True),
            self.dialect,
        )
        try:
            with database_connection(self.target, database) as conn:
                row = conn.execute(text(f"SHOW CREATE {keyword} {q}")).fetchone()
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="capture_object_ddl", target=self.target,
                extra={"database": database, "object_type": object_type},
            )
        if row is None:
            raise AppHttpException(
                message="El objeto no existe en la base de datos.",
                status_code=404,
                context={"object_type": object_type, "name": name},
            )
        ddl = self._show_create_value(row, candidates, fallback_idx)
        if not ddl or not str(ddl).strip():
            # Un SHOW CREATE que devuelve vacío ocurre cuando el usuario no tiene permiso
            # para ver el cuerpo. Recrear con eso destruiría el objeto: fail-closed.
            raise AppHttpException(
                message=(
                    "El motor no devolvió el DDL del objeto (probable falta de permiso para "
                    "ver su cuerpo); no se recreará."
                ),
                status_code=409,
                context={"object_type": object_type, "name": name},
            )
        return str(ddl)

    def routine_grants(
        self, database: str, routine_type: str, name: str
    ) -> list[RoutineGrantInfo]:
        """
        Privilegios a nivel de RUTINA sobre ``database.name``, leídos de
        ``mysql.procs_priv``.

        Hace falta porque MySQL/MariaDB los BORRAN al dropear la rutina. La doc de MySQL es
        explícita: "MySQL does not automatically revoke any privileges when you drop a
        database or table. However, if you drop a routine, any routine-level privileges
        granted for that routine are revoked." MariaDB dice lo mismo. Es una ASIMETRÍA real
        con las tablas (un DROP TABLE + CREATE TABLE sí conserva sus grants).

        ``mysql.procs_priv`` es la ÚNICA fuente directa: ``information_schema`` NO tiene
        tabla de privilegios de rutina (llega hasta ``COLUMN_PRIVILEGES`` y se saltea las
        rutinas). Si no es legible, el fallback recorre ``SHOW GRANTS`` por usuario.
        """
        kind = "FUNCTION" if str(routine_type).upper() in ("FUNCTION", "FUNC") else "PROCEDURE"
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        validate_identifier(name, self.dialect, "rutina", allow_existing=True)
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(
                    text(
                        "SELECT Host, User, Proc_priv FROM mysql.procs_priv "
                        "WHERE Db = :db AND Routine_name = :n AND Routine_type = :t"
                    ),
                    {"db": database, "n": name, "t": kind},
                ).fetchall()
        except SQLAlchemyError:
            return self._routine_grants_via_show(database, kind, name)
        out: list[RoutineGrantInfo] = []
        for host, user, proc_priv in rows:
            tokens = [t.strip().lower() for t in str(proc_priv or "").split(",") if t.strip()]
            privs = sorted({self._PROCS_PRIV_MAP[t] for t in tokens if t in self._PROCS_PRIV_MAP})
            grant_option = "grant" in tokens
            if not privs and not grant_option:
                continue
            out.append(
                RoutineGrantInfo(
                    username=str(user), host=str(host or "%"), routine_type=kind,
                    routine_name=name, privileges=privs, grant_option=grant_option,
                )
            )
        return out

    def _routine_grants_via_show(
        self, database: str, kind: str, name: str
    ) -> list[RoutineGrantInfo]:
        """
        Fallback de ``routine_grants`` cuando ``mysql.procs_priv`` no es legible: recorre
        ``SHOW GRANTS FOR`` de cada cuenta y filtra las líneas de ESTA rutina.

        Es más caro (una consulta por cuenta) y menos preciso, así que es el plan B. Si
        TAMBIÉN falla, propaga la excepción: el controller la traduce en "no se pudieron
        leer los grants" y NO dropea la rutina (ver ``CollationConversionController``) —
        dropearla a ciegas destruiría privilegios sin posibilidad de restaurarlos.
        """
        target_a = f"{quote_identifier(database, self.dialect)}.{quote_identifier(name, self.dialect)}"
        target_b = f"{database}.{name}"
        out: list[RoutineGrantInfo] = []
        with server_connection(self.target) as conn:
            accounts = conn.execute(
                text(
                    "SELECT User, Host FROM mysql.user "
                    f"WHERE User NOT IN ({_in_list(_SYSTEM_USERS)})"
                )
            ).fetchall()
            for user, host in accounts:
                try:
                    grantee = self._user_at_host(str(user), str(host or "%"))
                    lines = conn.execute(text(f"SHOW GRANTS FOR {grantee}")).fetchall()
                except SQLAlchemyError:
                    continue  # cuenta ilegible/borrada entre consultas: no bloquea al resto
                for row in lines:
                    line = str(row[0])
                    upper = line.upper()
                    if f" ON {kind} " not in upper:
                        continue
                    if target_a not in line and target_b not in line:
                        continue
                    privs = sorted(
                        {
                            canon
                            for token, canon in self._PROCS_PRIV_MAP.items()
                            if token.upper() in upper
                        }
                    )
                    if "ALL PRIVILEGES" in upper:
                        privs = sorted(set(self._PROCS_PRIV_MAP.values()))
                    if not privs and "WITH GRANT OPTION" not in upper:
                        continue
                    out.append(
                        RoutineGrantInfo(
                            username=str(user), host=str(host or "%"), routine_type=kind,
                            routine_name=name, privileges=privs,
                            grant_option="WITH GRANT OPTION" in upper,
                        )
                    )
        return out

    def apply_routine_grants(self, database: str, grants: list[RoutineGrantInfo]) -> int:
        """
        Reaplica los privilegios de rutina capturados antes del DROP.

        ``GRANT ... ON PROCEDURE|FUNCTION `db`.`rutina` TO 'u'@'h'`` es sintaxis válida en
        MySQL 8 y MariaDB (ambos documentan ``object_type: TABLE | FUNCTION | PROCEDURE`` y
        el ``priv_level`` ``db_name.routine_name``). Los privilegios salen del mapa cerrado
        ``_PROCS_PRIV_MAP`` (nunca del texto del motor) y el grantee pasa por whitelist +
        quoting como string literal, igual que el resto del DCL del adapter.
        """
        if not grants:
            return 0
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        db_q = quote_identifier(database, self.dialect)
        allowed = set(self._PROCS_PRIV_MAP.values())
        statements: list[str] = []
        for g in grants:
            kind = "FUNCTION" if g.routine_type.upper() == "FUNCTION" else "PROCEDURE"
            validate_identifier(g.username, self.dialect, "usuario", allow_existing=True)
            validate_host(g.host or "%")
            rname_q = quote_identifier(
                validate_identifier(g.routine_name, self.dialect, "rutina", allow_existing=True),
                self.dialect,
            )
            privs = [p for p in g.privileges if p in allowed]
            if not privs:
                # Solo GRANT OPTION: USAGE es el privilegio "vacío" con el que se confiere
                # la grant option sin otorgar nada más (mismo criterio que grant_object).
                privs = ["USAGE"] if g.grant_option else []
            if not privs:
                continue
            stmt = (
                f"GRANT {', '.join(privs)} ON {kind} {db_q}.{rname_q} "
                f"TO {self._user_at_host(g.username, g.host or '%')}"
            )
            if g.grant_option:
                stmt += " WITH GRANT OPTION"
            statements.append(stmt)
        if not statements:
            return 0
        self._execute_server(
            statements, op="apply_routine_grants", extra={"database": database}
        )
        return len(statements)

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

    def add_user_host(self, username, source_host, new_host, *, new_password=None) -> None:
        """
        Agrega un host a un usuario: crea ``'user'@'new_host'`` como cuenta nueva.

        - ``new_password`` con valor ⇒ ``CREATE USER ... IDENTIFIED BY '<nueva>'``.
        - ``new_password`` None ⇒ misma contraseña: se toma la sentencia que el propio
          motor emite con ``SHOW CREATE USER`` para la cuenta origen (escapa el hash de
          auth correctamente, incluso el binario de ``caching_sha2_password``) y solo se
          reescribe el grantee (host). No se descubre la contraseña en claro.
        """
        validate_identifier(username, self.dialect, "usuario", allow_existing=True)
        validate_host(source_host)
        validate_host(new_host)
        new_grantee = self._user_at_host(username, new_host)

        if new_password is not None:
            pwd = quote_string_literal(new_password, self.dialect)
            self._execute_server(
                [f"CREATE USER {new_grantee} IDENTIFIED BY {pwd}"],
                op="add_user_host",
                extra={"username": username, "host": new_host},
            )
            return

        source_grantee = self._user_at_host(username, source_host)
        try:
            with server_connection(self.target) as conn:
                row = conn.execute(text(f"SHOW CREATE USER {source_grantee}")).fetchone()
                if row is None:
                    raise AppHttpException(
                        message="La cuenta origen no existe en el motor.",
                        status_code=404,
                        context={"username": username, "host": source_host},
                    )
                create_stmt = row[0]
                prefix = f"CREATE USER {source_grantee}"
                # Los identificadores están whitelisteados (sin comillas ni backslash), así
                # que el grantee que emite el motor coincide byte a byte con el que armamos.
                if not create_stmt.startswith(prefix):
                    raise AppHttpException(
                        message=(
                            "No se pudo derivar la creación desde la cuenta origen; "
                            "reintenta indicando una contraseña nueva (reuse_password=false)."
                        ),
                        status_code=422,
                        context={"username": username},
                    )
                new_stmt = f"CREATE USER {new_grantee}" + create_stmt[len(prefix):]
                conn.execute(text(new_stmt))
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="add_user_host", target=self.target,
                extra={"username": username, "host": new_host},
            )

    def copy_user_grants(self, username, source_host, new_host) -> int:
        """
        Replica los GRANT de ``'user'@'source_host'`` a ``'user'@'new_host'``.

        Lee ``SHOW GRANTS FOR`` la cuenta origen y reejecuta cada sentencia reescribiendo
        únicamente el grantee. Es el MISMO servidor y motor que el gateway ya administra
        con pseudo-root (no se cruza una frontera de confianza nueva). Fail-closed: omite
        el ``USAGE`` base, los grants ``PROXY`` y cualquier sentencia con credencial
        embebida (``IDENTIFIED BY`` de motores viejos), que nunca se replica.
        """
        validate_identifier(username, self.dialect, "usuario", allow_existing=True)
        validate_host(source_host)
        validate_host(new_host)
        source_grantee = self._user_at_host(username, source_host)
        new_grantee = self._user_at_host(username, new_host)
        applied = 0
        try:
            with server_connection(self.target) as conn:
                rows = conn.execute(text(f"SHOW GRANTS FOR {source_grantee}")).fetchall()
                for r in rows:
                    stmt = self._rewrite_grant_line(str(r[0]), new_grantee)
                    if stmt is None:
                        continue
                    conn.execute(text(stmt))
                    applied += 1
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="copy_user_grants", target=self.target,
                extra={"username": username},
            )
        return applied

    @staticmethod
    def _rewrite_grant_line(line: str, new_grantee: str) -> str | None:
        """
        Reescribe una línea de ``SHOW GRANTS`` para apuntar al ``new_grantee``, o None si
        no debe replicarse. El grantee es lo que sigue al último `` TO``; después puede
        venir ``WITH GRANT OPTION``. Fail-closed: se omite el ``USAGE`` base (no confiere
        privilegio), los grants ``PROXY`` (sintaxis especial) y cualquier línea con
        credencial embebida (``IDENTIFIED BY`` de motores viejos), que nunca se replica.
        """
        idx = line.rfind(" TO ")
        if idx == -1:
            return None
        head = line[:idx]
        tail = line[idx + 4:]
        head_u = head.strip().upper()
        if head_u == "GRANT USAGE ON *.*" or head_u.startswith("GRANT PROXY ON"):
            return None
        if " IDENTIFIED BY " in line.upper():
            return None
        suffix = (
            " WITH GRANT OPTION"
            if tail.upper().rstrip().endswith("WITH GRANT OPTION")
            else ""
        )
        return f"{head} TO {new_grantee}{suffix}"

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

    def _estimate_rows(self, conn, table: str, schema: str) -> int | None:
        # ``TABLE_ROWS`` es NULL para objetos donde no aplica (vistas, algunos motores) y
        # sale de una caché gobernada por ``information_schema_stats_expiry``, así que puede
        # estar atrasada. NULL => desconocido, no cero.
        row = conn.execute(
            text(
                "SELECT TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"
            ),
            {"s": schema, "t": table},
        ).scalar()
        return int(row) if row is not None else None

    # ------------------------- snapshot canónico (hooks) ---------------------- #
    def _column_extras(self, conn, database, table, schema) -> dict[str, dict]:
        """
        Collation/charset/on_update/column_type por columna desde
        ``information_schema.COLUMNS``: el Inspector de SQLAlchemy no expone estos de forma
        fiable en MySQL/MariaDB. En particular ``str(reflected_type)`` PIERDE detalle crítico
        del tipo (``ENUM``/``SET`` sin su lista de valores → DDL inválido; ``UNSIGNED`` →
        rango corrupto; display width). ``COLUMN_TYPE`` es la fuente CANÓNICA del tipo exacto
        (``enum('a','b')``, ``bigint(20) unsigned``, ``tinyint(1)``, …); ``base_adapter`` la
        usa en vez de ``str(type)`` cuando está presente.
        """
        out: dict[str, dict] = {}
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME, COLLATION_NAME, CHARACTER_SET_NAME, EXTRA, COLUMN_TYPE, "
                "GENERATION_EXPRESSION, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t"
            ),
            {"db": database, "t": table},
        ).fetchall()
        for name, coll, cs, extra, column_type, gen_expr, col_comment in rows:
            on_update = None
            if extra and "on update" in str(extra).lower():
                on_update = "CURRENT_TIMESTAMP"
            out[name] = {
                "collation": coll,
                "charset": cs,
                "on_update": on_update,
                "column_type": str(column_type) if column_type else None,
                # Expresión CANÓNICA de la columna generada, tomada de
                # ``GENERATION_EXPRESSION`` (sin los paréntesis externos de ``AS (...)``,
                # que el render agrega). Es la fuente correcta frente a la reflexión de
                # ``SHOW CREATE TABLE`` de SQLAlchemy, cuyo parser cuenta paréntesis sin
                # entender los literales de string: un ``COMMENT '...( )...'`` en una
                # columna generada le hace capturar de más (se traga ``VIRTUAL``/``STORED``
                # y el propio COMMENT) → DDL inválido. En columnas no generadas es ``''``.
                "generation_expression": (str(gen_expr) if gen_expr else None),
                # ``COLUMN_COMMENT`` es autoritativo: recupera el comentario que la misma
                # captura contaminada de SQLAlchemy perdía en las columnas generadas.
                "comment": (str(col_comment) if col_comment else None),
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

    def _database_defaults(self, conn, database, schema) -> dict[str, str | None]:
        """Default de charset/collation de la BD desde ``information_schema.SCHEMATA``."""
        db_row = conn.execute(
            text(
                "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :db"
            ),
            {"db": database},
        ).fetchone()
        if not db_row:
            return {}
        return {
            "db_charset": str(db_row[0]) if db_row[0] else None,
            "db_collation": str(db_row[1]) if db_row[1] else None,
        }

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
    # Tokens que una expresión de generación LIMPIA nunca debería contener: si aparecen
    # es señal de que la captura se contaminó (típicamente la reflexión de ``SHOW CREATE``
    # de SQLAlchemy tragándose ``VIRTUAL``/``STORED``/el ``COMMENT`` por paréntesis en el
    # comentario). Se comparan en mayúsculas contra la expresión completa.
    _CONTAMINATED_GENEXPR_TOKENS = (
        " VIRTUAL",
        " STORED",
        " COMMENT ",
        "GENERATED ALWAYS",
    )

    def _guard_generation_expression(self, col) -> None:
        """
        Falla ANTES de tocar el motor si la expresión de una columna generada está
        malformada. Sin esto, un ``sqltext`` corrupto se emitía dentro de
        ``GENERATED ALWAYS AS (...)`` y el ``CREATE TABLE`` reventaba en el motor
        (1064) a mitad de un lote — coherente con la política fail-closed del módulo.
        """
        sqltext = col.computed.sqltext or ""
        # Ambas verificaciones corren sobre el texto ENMASCARADO: un paréntesis o la
        # palabra ``VIRTUAL`` DENTRO de un literal ('(' , ' VIRTUAL') son contenido
        # legítimo, y analizarlos como sintaxis daría un falso positivo que bloquearía
        # una columna válida. Es la misma trampa que causó el bug que este guard cubre.
        probe = mask_quoted_spans(sqltext)
        # 1) Paréntesis balanceados (un desbalance rompe el ``AS (...)``).
        depth = 0
        for ch in probe:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    break
        balanced = depth == 0
        # 2) La expresión no debe arrastrar la persistencia ni el COMMENT.
        upper = probe.upper()
        contaminated = any(tok in upper for tok in self._CONTAMINATED_GENEXPR_TOKENS)
        if not balanced or contaminated:
            raise AppHttpException(
                message=(
                    f"La expresión de la columna generada {col.name!r} quedó malformada "
                    "al capturarla (paréntesis desbalanceados o contaminada con la "
                    "persistencia/COMMENT). No se puede reconstruir el DDL de forma segura."
                ),
                status_code=422,
                context={"column": col.name, "sqltext": sqltext},
            )

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
            self._guard_generation_expression(col)
            stored = "STORED" if col.computed.persisted else "VIRTUAL"
            parts.append(f"GENERATED ALWAYS AS ({col.computed.sqltext}) {stored}")
            if not col.nullable:
                parts.append("NOT NULL")
        else:
            parts.append("NULL" if col.nullable else "NOT NULL")
            inline_on_update = False
            if col.default is not None:
                default = col.default
                # MariaDB refleja la cláusula ``ON UPDATE …`` DENTRO del COLUMN_DEFAULT
                # de columnas DATETIME/TIMESTAMP (SQLAlchemy la devuelve pegada al default).
                # ``on_update`` se emite por separado más abajo → la quitamos del default
                # para no duplicar la cláusula y generar un CREATE TABLE inválido.
                idx = default.upper().find(" ON UPDATE ")
                if idx != -1:
                    default = default[:idx].rstrip()
                    inline_on_update = True
                parts.append(f"DEFAULT {default}")
            if col.on_update or inline_on_update:
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
        # MySQL/MariaDB (InnoDB): la columna AUTO_INCREMENT debe ser la primera
        # columna de ALGUNA clave definida en esta MISMA sentencia (un índice
        # creado en una sentencia posterior no sirve para esta validación de
        # motor). Si la PK/UNIQUE inline no la cubre —origen con PK compuesta
        # heredada donde el autoincrement no encabeza la clave— agregamos una
        # KEY de apoyo: no cambia la PK ni ningún otro objeto, solo satisface
        # el requisito del motor. Nombre EXPLÍCITO con prefijo ``_gw_`` (mismo
        # patrón que ``_gw_v_``/``_gw_stg_`` en otros módulos): sin nombre,
        # MySQL/MariaDB la auto-nombra igual que la columna (``id``) — si el
        # origen YA tiene un índice real sobre esa columna (con ese mismo
        # nombre auto-asignado), la sentencia posterior que lo recrea en el
        # destino choca con ``1061 Duplicate key name``.
        auto_col = next((c.name for c in tbl.columns if c.autoincrement), None)
        if auto_col:
            leads_pk = bool(tbl.primary_key) and tbl.primary_key[0] == auto_col
            leads_unique = any(
                uc.columns and uc.columns[0] == auto_col for uc in tbl.unique_constraints
            )
            if not leads_pk and not leads_unique:
                key_name = self._q(f"_gw_autoinc_{auto_col}"[:64], "indice")
                lines.append(f"KEY {key_name} ({self._q(auto_col, 'columna')})")
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
