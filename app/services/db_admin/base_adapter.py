"""
Contrato común de los adaptadores de servidor.

`ServerAdapter` define las operaciones que el gateway ejecuta contra un servidor
destino. La introspección (read-only) y test_connection son concretas aquí porque
el `Inspector` de SQLAlchemy es cross-dialect y nunca lee filas. Las operaciones
específicas de cada motor (listar BDs/usuarios, DDL/DCL) son abstractas.

Las operaciones de ESCRITURA (create/drop database/user, grants) están definidas
en el contrato e implementadas por cada subclase, pero NO se exponen vía API en la
Iteración 1 (solo se usarán a partir de la Iteración 2).
"""

import re
from abc import ABC, abstractmethod

from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.core.remote_engine import (
    ServerTarget,
    database_connection,
    map_driver_error,
    server_connection,
)
from app.exceptions import AppHttpException
from app.services.db_admin import snapshot_data
from app.services.db_admin.dtos import (
    CheckConstraintInfo,
    ColumnInfo,
    ComputedInfo,
    ConnectionInfo,
    EngineUserInfo,
    EnumTypeInfo,
    EventInfo,
    ExtensionInfo,
    ForeignKeyInfo,
    GrantInfo,
    GrantLevel,
    IdentityInfo,
    IndexInfo,
    ObjectRef,
    RoutineInfo,
    SchemaSnapshot,
    SeedResult,
    SequenceInfo,
    StructureDump,
    TableSchema,
    TableStat,
    TriggerInfo,
    UniqueConstraintInfo,
    ViewInfo,
)
from app.services.db_admin.identifiers import quote_identifier, validate_identifier
from app.services.db_admin.schema_diff import (
    DiffItem,
    RenderedStatement,
    SchemaDiff,
)


class ServerAdapter(ABC):
    dialect: str

    # ¿El motor modela usuarios por par ``'user'@'host'`` (varios hosts por username)?
    # MySQL/MariaDB: True. PostgreSQL: False (un rol no tiene host; el acceso por host se
    # controla en pg_hba.conf, fuera del alcance SQL). Gobierna la vista agrupada y si se
    # permite "agregar host".
    supports_hosts: bool = True

    # Tipos de rutina admitidos en grants de EXECUTE/ALTER ROUTINE.
    _ROUTINE_KINDS = frozenset({"FUNCTION", "PROCEDURE"})

    def __init__(self, target: ServerTarget):
        self.target = target

    # ---- Helpers de validación de object_ref (compartidos por los adapters) --- #
    @staticmethod
    def _require_field(value: str | None, kind: str) -> str:
        if not value:
            raise AppHttpException(
                message=f"Falta '{kind}' para la operación de permiso.",
                status_code=422,
                context={"missing": kind},
            )
        return value

    @classmethod
    def _routine_kind(cls, routine) -> str:
        if routine is None:
            raise AppHttpException(
                message="Falta la rutina (routine) para el grant.", status_code=422
            )
        kind = (routine.kind or "").upper()
        if kind not in cls._ROUTINE_KINDS:
            raise AppHttpException(
                message="Tipo de rutina inválido (use FUNCTION o PROCEDURE).",
                status_code=422,
                context={"allowed": sorted(cls._ROUTINE_KINDS)},
            )
        return kind

    # ------------------------------------------------------------------ #
    # Snapshot: sanitización de DEFINER/owner (compartida; Plan 09)       #
    # ------------------------------------------------------------------ #
    # MySQL: DEFINER=`user`@`host`  |  SQL SECURITY DEFINER (vistas/rutinas/triggers).
    _DEFINER_RE = re.compile(
        r"\s+DEFINER\s*=\s*(`[^`]*`@`[^`]*`|'[^']*'@'[^']*'|\"[^\"]*\"@\"[^\"]*\"|\S+)",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_definer_clause(cls, ddl: str) -> str:
        """
        Quita la cláusula ``DEFINER=...`` de un DDL capturado (MySQL/MariaDB).

        Capturar el DEFINER literal haría fallar el re-apply en otro servidor donde ese
        usuario no existe. Tras quitarlo, el motor usa el invocador/owner del destino.
        ``SQL SECURITY DEFINER`` se deja intacto (es válido y no referencia un usuario
        concreto); el riesgo de escalada se documenta para revisión del admin.
        """
        return cls._DEFINER_RE.sub("", ddl)

    # ------------------------------------------------------------------ #
    # Específico de dialecto                                              #
    # ------------------------------------------------------------------ #
    @abstractmethod
    def _version_sql(self) -> str:
        """Sentencia que devuelve la versión del servidor."""

    @abstractmethod
    def _inspect_schema(self, database: str) -> str:
        """Schema que el Inspector debe usar para esta BD (MySQL: la BD; PG: 'public')."""

    @abstractmethod
    def list_databases(self) -> list[str]:
        """Lista BDs reales del servidor, excluyendo las del sistema."""

    @abstractmethod
    def list_users(self) -> list[EngineUserInfo]:
        """Lista usuarios/roles del motor, excluyendo los internos."""

    @abstractmethod
    def dump_structure(self, database: str) -> "StructureDump":
        """
        Dump estructural COMPLETO de la BD (tablas, vistas, rutinas, triggers, y
        según motor: secuencias, tipos, extensiones, events). SOLO estructura, jamás
        filas. Las sentencias vienen YA en orden de dependencia para re-aplicarse.
        Plan 09 (adopción + snapshot como blueprint baseline).
        """

    @abstractmethod
    def _estimate_rows(self, conn, table: str, schema: str) -> int:
        """
        Estimación de filas de una tabla desde el catálogo (rápida, aproximada; NO
        cuenta filas). MySQL: ``information_schema.TABLES.TABLE_ROWS``; PostgreSQL:
        ``pg_class.reltuples``. Solo para informar la selección de datos-semilla.
        """

    # ------------------------------------------------------------------ #
    # Escritura (contrato; uso por API a partir de la Iteración 2)        #
    # ------------------------------------------------------------------ #
    @abstractmethod
    def create_database(
        self, db_name: str, charset: str | None = None, collation: str | None = None,
        owner: str | None = None,
    ) -> None: ...

    @abstractmethod
    def drop_database(self, db_name: str) -> None: ...

    @abstractmethod
    def create_user(self, username: str, password: str, host: str = "%") -> None: ...

    @abstractmethod
    def drop_user(self, username: str, host: str = "%") -> None: ...

    @abstractmethod
    def change_password(self, username: str, new_password: str, host: str = "%") -> None: ...

    def add_user_host(
        self,
        username: str,
        source_host: str,
        new_host: str,
        *,
        new_password: str | None = None,
    ) -> None:
        """
        Clona una cuenta existente a un ``new_host`` (agregar host a un usuario).

        Solo tiene sentido en motores con ``supports_hosts=True`` (MySQL/MariaDB): ahí
        ``'user'@'hostA'`` y ``'user'@'hostB'`` son cuentas separadas. ``new_password``
        None ⇒ se copia el hash de la cuenta origen (misma contraseña, sin conocerla en
        claro); con valor ⇒ se fija esa contraseña nueva. El default rechaza (422); cada
        motor que lo soporte sobreescribe.
        """
        raise AppHttpException(
            message="Este motor no soporta múltiples hosts por usuario (no aplica 'agregar host').",
            status_code=422,
            context={"dialect": self.dialect},
        )

    def copy_user_grants(self, username: str, source_host: str, new_host: str) -> int:
        """
        Replica los permisos de ``'user'@'source_host'`` a ``'user'@'new_host'`` (mismo
        servidor/motor). Best-effort: omite el USAGE base y privilegios no portables por
        seguridad. Devuelve cuántas sentencias GRANT se aplicaron. Default: 422.
        """
        raise AppHttpException(
            message="Este motor no soporta copiar grants entre hosts de un usuario.",
            status_code=422,
            context={"dialect": self.dialect},
        )

    @abstractmethod
    def grant_database(
        self, username: str, db_name: str, host: str = "%", privileges: str = "ALL PRIVILEGES",
    ) -> None: ...

    @abstractmethod
    def revoke_database(
        self, username: str, db_name: str, host: str = "%", privileges: str = "ALL PRIVILEGES",
    ) -> None: ...

    # ---- GRANT/REVOKE GRANULAR (Plan 07) — por nivel de objeto ---------------- #
    @abstractmethod
    def grant_object(
        self,
        grantee: EngineUserInfo,
        level: GrantLevel,
        object_ref: ObjectRef,
        privileges: list[str],
        *,
        with_grant_option: bool = False,
    ) -> None:
        """Otorga ``privileges`` al ``grantee`` sobre el objeto del ``object_ref``."""

    @abstractmethod
    def revoke_object(
        self,
        grantee: EngineUserInfo,
        level: GrantLevel,
        object_ref: ObjectRef,
        privileges: list[str],
        *,
        cascade: bool = False,
    ) -> None:
        """
        Revoca ``privileges`` del ``grantee`` sobre el objeto del ``object_ref``.

        ``cascade`` solo aplica a PostgreSQL (revoca en cascada los privilegios que el
        ``grantee`` haya delegado a su vez). En MySQL/MariaDB no existe y debe
        rechazarse. Por defecto ``RESTRICT`` (no cascada).
        """

    @abstractmethod
    def list_grants(
        self, grantee: EngineUserInfo, database: str | None = None
    ) -> list[GrantInfo]:
        """
        Introspecciona los privilegios efectivos del ``grantee``. En PostgreSQL los
        grants de objeto son POR BASE DE DATOS: ``database`` es necesario para ver
        tablas/columnas/secuencias/rutinas; en MySQL/MariaDB se ignora (info_schema
        es a nivel servidor).
        """

    @abstractmethod
    def can_grant(
        self, level: GrantLevel, object_ref: ObjectRef, privileges: list[str]
    ) -> bool:
        """
        ¿La credencial del gateway (grantor) puede DELEGAR ``privileges`` sobre el
        objeto? Pre-chequeo de capability: superusuario/owner o privilegio con grant
        option. Se consulta ANTES de ejecutar el GRANT (el error del motor es la red
        secundaria).
        """

    def reassign_database_owner(
        self,
        db_name: str,
        new_owner: str,
        *,
        new_host: str = "%",
        old_owner: str | None = None,
        old_host: str = "%",
    ) -> None:
        """
        Reasigna la propiedad de una BD al usuario ``new_owner``.

        Implementación por defecto (propiedad LÓGICA vía privilegios, válida para
        MySQL/MariaDB): revoca al propietario anterior (si se indica) y otorga al
        nuevo. PostgreSQL la sobreescribe para usar OWNER nativo (ALTER DATABASE).
        La semántica de "propiedad" es específica de cada motor, por eso vive en el
        adapter y nunca en el controller.
        """
        if old_owner:
            self.revoke_database(old_owner, db_name, host=old_host)
        self.grant_database(new_owner, db_name, host=new_host)

    # ------------------------------------------------------------------ #
    # Concreto: conexión e introspección (read-only, cross-dialect)       #
    # ------------------------------------------------------------------ #
    def test_connection(self) -> ConnectionInfo:
        try:
            with server_connection(self.target) as conn:
                version = conn.execute(text(self._version_sql())).scalar()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="test_connection", target=self.target)
        return ConnectionInfo(
            ok=True,
            dialect=self.dialect,
            server_version=str(version) if version is not None else None,
        )

    def list_tables(self, database: str) -> list[str]:
        # Introspección de un objeto PREEXISTENTE: whitelist ampliada (nombres legados).
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                return sorted(inspect(conn).get_table_names(schema=schema))
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_tables", target=self.target, extra={"database": database}
            )

    def get_table_schema(self, database: str, table: str) -> TableSchema:
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        validate_identifier(table, self.dialect, "tabla", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                insp = inspect(conn)
                try:
                    return self._build_table_schema(insp, conn, database, table, schema)
                except NoSuchTableError:
                    raise AppHttpException(
                        message="La tabla no existe en la base de datos indicada.",
                        status_code=404,
                        context={"database": database, "table": table},
                    )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc,
                op="get_table_schema",
                target=self.target,
                extra={"database": database, "table": table},
            )

    # ------------------------------------------------------------------ #
    # Construcción de TableSchema extendido (compartido; usa el Inspector) #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _index_from_raw(ix: dict) -> IndexInfo:
        """Traduce un índice del Inspector a IndexInfo, sin descartar dialect_options."""
        dialect_opts = ix.get("dialect_options") or {}
        method = None
        predicate = None
        include_columns: list[str] = []
        for key, val in dialect_opts.items():
            if key.endswith("_using") and val:
                method = str(val)
            elif key.endswith("_where") and val:
                predicate = str(val)
            elif key.endswith("_include") and val:
                include_columns = list(val)
        # column_sorting: {col: ('desc', 'nulls_first')} solo cuando no es el default.
        column_sort: dict[str, list[str]] = {}
        for col, opts in (ix.get("column_sorting") or {}).items():
            if opts:
                column_sort[col] = list(opts)
        # expressions (índice funcional): SQLAlchemy las expone en 'expressions' cuando
        # alguna posición de column_names es None (es una expresión, no una columna).
        raw_names = ix.get("column_names") or []
        expressions = []
        if None in raw_names:
            expressions = [str(e) for e in (ix.get("expressions") or []) if e is not None]
        cols = [c for c in raw_names if c is not None]
        return IndexInfo(
            name=ix.get("name"),
            columns=cols,
            unique=bool(ix.get("unique")),
            method=method,
            predicate=predicate,
            expressions=expressions,
            column_sort=column_sort,
            include_columns=include_columns,
        )

    def _build_table_schema(
        self, insp, conn, database: str, table: str, schema: str
    ) -> TableSchema:
        """
        Construye un ``TableSchema`` COMPLETO reutilizando lo que el Inspector ya expone
        (columnas + computed/identity, FKs + options, índices + dialect_options, checks,
        uniques, comment) más los hooks por adapter (collation/charset/on_update de
        columna y storage_options de tabla) para lo que el Inspector no expone fiable.
        """
        columns_raw = insp.get_columns(table, schema=schema)
        pk = insp.get_pk_constraint(table, schema=schema)
        pk_cols = pk.get("constrained_columns") or []
        pk_name = pk.get("name")
        fks_raw = insp.get_foreign_keys(table, schema=schema)
        idx_raw = insp.get_indexes(table, schema=schema)
        try:
            checks_raw = insp.get_check_constraints(table, schema=schema)
        except (NotImplementedError, SQLAlchemyError):
            checks_raw = []
        try:
            uniques_raw = insp.get_unique_constraints(table, schema=schema)
        except (NotImplementedError, SQLAlchemyError):
            uniques_raw = []
        try:
            comment = (insp.get_table_comment(table, schema=schema) or {}).get("text")
        except (NotImplementedError, SQLAlchemyError):
            comment = None

        extras = self._column_extras(conn, database, table, schema)
        storage = self._table_storage_options(conn, database, table, schema)

        pk_set = set(pk_cols)
        columns: list[ColumnInfo] = []
        for c in columns_raw:
            ex = extras.get(c["name"], {})
            computed = None
            comp_raw = c.get("computed")
            if comp_raw:
                computed = ComputedInfo(
                    sqltext=str(comp_raw.get("sqltext") or ""),
                    persisted=bool(comp_raw.get("persisted")),
                )
            identity = None
            id_raw = c.get("identity")
            if id_raw:
                identity = IdentityInfo(
                    always=bool(id_raw.get("always")),
                    start=id_raw.get("start"),
                    increment=id_raw.get("increment"),
                )
            columns.append(
                ColumnInfo(
                    name=c["name"],
                    type=str(c["type"]),
                    nullable=bool(c.get("nullable", True)),
                    default=None if c.get("default") is None else str(c.get("default")),
                    primary_key=c["name"] in pk_set,
                    autoincrement=c.get("autoincrement") in (True, "auto"),
                    comment=c.get("comment"),
                    collation=ex.get("collation"),
                    charset=ex.get("charset"),
                    computed=computed,
                    identity=identity,
                    on_update=ex.get("on_update"),
                )
            )
        foreign_keys = [
            ForeignKeyInfo(
                name=fk.get("name"),
                columns=fk.get("constrained_columns") or [],
                referred_table=fk.get("referred_table") or "",
                referred_columns=fk.get("referred_columns") or [],
                referred_schema=fk.get("referred_schema"),
                on_delete=(fk.get("options") or {}).get("ondelete"),
                on_update=(fk.get("options") or {}).get("onupdate"),
                deferrable=(fk.get("options") or {}).get("deferrable"),
                initially=(fk.get("options") or {}).get("initially"),
            )
            for fk in fks_raw
        ]
        indexes = [self._index_from_raw(ix) for ix in idx_raw]
        check_constraints = [
            CheckConstraintInfo(name=ck.get("name"), sqltext=str(ck.get("sqltext") or ""))
            for ck in checks_raw
            if ck.get("sqltext")
        ]
        unique_constraints = [
            UniqueConstraintInfo(
                name=uc.get("name"), columns=uc.get("column_names") or []
            )
            for uc in uniques_raw
        ]
        return TableSchema(
            database=database,
            table=table,
            columns=columns,
            primary_key=list(pk_cols),
            primary_key_name=pk_name,
            foreign_keys=foreign_keys,
            indexes=indexes,
            check_constraints=check_constraints,
            unique_constraints=unique_constraints,
            comment=comment,
            storage_options=storage,
        )

    # ------------------------------------------------------------------ #
    # Snapshot estructural CANÓNICO (Plan diff) — SchemaSnapshot           #
    # ------------------------------------------------------------------ #
    def structural_snapshot(self, database: str) -> SchemaSnapshot:
        """
        Snapshot estructural canónico y COMPLETO de la BD (entrada del motor de diff).

        Reutiliza el Inspector para tablas y los hooks por adapter para vistas/rutinas/
        triggers/secuencias/tipos/extensiones/events. Solo estructura, jamás filas.
        PostgreSQL cubre solo el schema ``public`` (limitación conocida del sistema).
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                insp = inspect(conn)
                tables = [
                    self._build_table_schema(insp, conn, database, t, schema)
                    for t in sorted(insp.get_table_names(schema=schema))
                ]
                views = self._snapshot_views(conn, database, schema)
                routines = self._snapshot_routines(conn, database, schema)
                triggers = self._snapshot_triggers(conn, database, schema)
                sequences = self._snapshot_sequences(conn, database, schema)
                enum_types = self._snapshot_enum_types(conn, database, schema)
                extensions = self._snapshot_extensions(conn, database, schema)
                events = self._snapshot_events(conn, database, schema)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="structural_snapshot", target=self.target,
                extra={"database": database},
            )
        return SchemaSnapshot(
            database=database,
            source_engine=self.dialect,
            tables=tables,
            views=views,
            routines=routines,
            triggers=triggers,
            sequences=sequences,
            enum_types=enum_types,
            extensions=extensions,
            events=events,
        )

    # ---- Hooks del snapshot (default vacío; cada adapter sobreescribe) --- #
    def _column_extras(self, conn, database: str, table: str, schema: str) -> dict[str, dict]:
        """{col: {collation, charset, on_update}} — lo que el Inspector no expone fiable."""
        return {}

    def _table_storage_options(self, conn, database: str, table: str, schema: str) -> dict[str, str]:
        """engine/charset/collation por tabla + db_charset/db_collation (herencia)."""
        return {}

    def _snapshot_views(self, conn, database: str, schema: str) -> list[ViewInfo]:
        return []

    def _snapshot_routines(self, conn, database: str, schema: str) -> list[RoutineInfo]:
        return []

    def _snapshot_triggers(self, conn, database: str, schema: str) -> list[TriggerInfo]:
        return []

    def _snapshot_sequences(self, conn, database: str, schema: str) -> list[SequenceInfo]:
        return []

    def _snapshot_enum_types(self, conn, database: str, schema: str) -> list[EnumTypeInfo]:
        return []

    def _snapshot_extensions(self, conn, database: str, schema: str) -> list[ExtensionInfo]:
        return []

    def _snapshot_events(self, conn, database: str, schema: str) -> list[EventInfo]:
        return []

    # ------------------------------------------------------------------ #
    # Generación de DDL desde un SchemaDiff (Plan diff — Fase 3)           #
    # ------------------------------------------------------------------ #
    # El diff YA viene ordenado por fase (1..9). Cada RenderedStatement lleva los
    # flags de riesgo calculados por el motor de diff (Fase 2). Los identificadores
    # derivados del motor origen se revalidan (validate_identifier, allow_existing) y
    # se re-emiten con quote_identifier: nunca se interpola texto crudo.
    def render_diff(self, diff: SchemaDiff) -> list[RenderedStatement]:
        """Traduce un ``SchemaDiff`` a sentencias DDL para el motor de este adapter."""
        out: list[RenderedStatement] = []
        for item in diff.items:
            out.extend(self._render_item(item))
        return out

    def _q(self, name: str, kind: str = "objeto") -> str:
        return quote_identifier(
            validate_identifier(name, self.dialect, kind, allow_existing=True), self.dialect
        )

    def _stmt(
        self, item: DiffItem, sql: str, *, down_sql: str | None = None,
        down_confirmed: bool = False,
    ) -> RenderedStatement:
        return RenderedStatement(
            sql=sql,
            object_type=item.object_type,
            object_name=item.object_name,
            change_type=item.change_type,
            phase=item.phase,
            risk=item.risk,
            down_sql=down_sql,
            down_confirmed=down_confirmed,
        )

    def _render_item(self, item: DiffItem) -> list[RenderedStatement]:
        ot, ct = item.object_type, item.change_type
        handler = {
            ("table", "new"): self._ri_table_new,
            ("table", "dropped"): self._ri_table_dropped,
            ("column", "new"): self._ri_column_new,
            ("column", "dropped"): self._ri_column_dropped,
            ("column", "modified"): self._ri_column_modified,
            ("primary_key", "new"): self._ri_pk_changed,
            ("primary_key", "modified"): self._ri_pk_changed,
            ("primary_key", "dropped"): self._ri_pk_changed,
            ("foreign_key", "new"): self._ri_fk_new,
            ("foreign_key", "modified"): self._ri_fk_modified,
            ("foreign_key", "dropped"): self._ri_fk_dropped,
            ("unique_constraint", "new"): self._ri_unique_new,
            ("unique_constraint", "modified"): self._ri_unique_modified,
            ("unique_constraint", "dropped"): self._ri_unique_dropped,
            ("check_constraint", "new"): self._ri_check_new,
            ("check_constraint", "modified"): self._ri_check_modified,
            ("check_constraint", "dropped"): self._ri_check_dropped,
            ("index", "new"): self._ri_index_new,
            ("index", "modified"): self._ri_index_modified,
            ("index", "dropped"): self._ri_index_dropped,
            ("view", "new"): self._ri_view_upsert,
            ("view", "modified"): self._ri_view_upsert,
            ("view", "dropped"): self._ri_view_dropped,
            ("materialized_view", "new"): self._ri_view_upsert,
            ("materialized_view", "modified"): self._ri_view_upsert,
            ("materialized_view", "dropped"): self._ri_view_dropped,
            ("routine", "new"): self._ri_routine_upsert,
            ("routine", "modified"): self._ri_routine_upsert,
            ("routine", "dropped"): self._ri_routine_dropped,
            ("trigger", "new"): self._ri_trigger_upsert,
            ("trigger", "modified"): self._ri_trigger_upsert,
            ("trigger", "dropped"): self._ri_trigger_dropped,
            ("event", "new"): self._ri_event_upsert,
            ("event", "modified"): self._ri_event_upsert,
            ("event", "dropped"): self._ri_event_dropped,
            ("sequence", "new"): self._ri_sequence_new,
            ("sequence", "modified"): self._ri_sequence_modified,
            ("sequence", "dropped"): self._ri_sequence_dropped,
            ("enum_type", "new"): self._ri_enum_new,
            ("enum_type", "modified"): self._ri_enum_modified,
            ("enum_type", "dropped"): self._ri_enum_dropped,
            ("extension", "new"): self._ri_extension_new,
            ("extension", "dropped"): self._ri_extension_dropped,
        }.get((ot, ct))
        if handler is None:
            return []  # tipo/cambio no soportado en v1: se omite (nunca se inventa DDL)
        return handler(item)

    # ---- Portables (mismo SQL en ambos motores, solo cambia el quoting) ---- #
    def _ri_table_new(self, item: DiffItem) -> list[RenderedStatement]:
        tbl = item.source_payload
        sql = self._render_create_table(tbl)
        return [self._stmt(item, sql, down_sql=f"DROP TABLE {self._q(tbl.table, 'tabla')}",
                           down_confirmed=True)]

    def _ri_table_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, f"DROP TABLE {self._q(item.object_name, 'tabla')}")]

    def _ri_column_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, col = item.parent_table, item.source_payload
        coldef = self._render_column_def(col)
        sql = f"ALTER TABLE {self._q(table, 'tabla')} ADD COLUMN {coldef}"
        down = f"ALTER TABLE {self._q(table, 'tabla')} DROP COLUMN {self._q(col.name, 'columna')}"
        return [self._stmt(item, sql, down_sql=down, down_confirmed=True)]

    def _ri_column_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, col = item.parent_table, item.target_payload
        sql = f"ALTER TABLE {self._q(table, 'tabla')} DROP COLUMN {self._q(col.name, 'columna')}"
        # Reverso SUGERIDO (no confirmado): recrea la columna, pero los datos ya se perdieron.
        down = f"ALTER TABLE {self._q(table, 'tabla')} ADD COLUMN {self._render_column_def(col)}"
        return [self._stmt(item, sql, down_sql=down, down_confirmed=False)]

    def _ri_fk_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, fk = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_add_fk(table, fk),
                           down_sql=self._render_drop_fk(table, fk), down_confirmed=True)]

    def _ri_fk_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        drop = self._render_drop_fk(table, item.target_payload)
        add = self._render_add_fk(table, item.source_payload)
        return [
            self._stmt(item, drop),
            self._stmt(item, add,
                       down_sql=self._render_add_fk(table, item.target_payload)),
        ]

    def _ri_fk_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, fk = item.parent_table, item.target_payload
        return [self._stmt(item, self._render_drop_fk(table, fk),
                           down_sql=self._render_add_fk(table, fk), down_confirmed=False)]

    def _render_add_unique(self, table: str, uc) -> str:
        cols = ", ".join(self._q(c, "columna") for c in uc.columns)
        name = self._q(uc.name, "constraint") if uc.name else None
        clause = f"ADD CONSTRAINT {name} UNIQUE" if name else "ADD UNIQUE"
        return f"ALTER TABLE {self._q(table, 'tabla')} {clause} ({cols})"

    def _ri_unique_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, uc = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_add_unique(table, uc),
                           down_sql=self._render_drop_unique(table, uc), down_confirmed=True)]

    def _ri_unique_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        drop = self._render_drop_unique(table, item.target_payload)
        add = self._render_add_unique(table, item.source_payload)
        return [
            self._stmt(item, drop),
            self._stmt(item, add,
                       down_sql=self._render_add_unique(table, item.target_payload)),
        ]

    def _ri_unique_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_unique(item.parent_table, item.target_payload))]

    def _render_add_check(self, table: str, ck) -> str:
        name = self._q(ck.name, "constraint") if ck.name else None
        clause = f"ADD CONSTRAINT {name} CHECK" if name else "ADD CHECK"
        return f"ALTER TABLE {self._q(table, 'tabla')} {clause} ({ck.sqltext})"

    def _ri_check_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, ck = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_add_check(table, ck),
                           down_sql=self._render_drop_check(table, ck), down_confirmed=True)]

    def _ri_check_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        drop = self._render_drop_check(table, item.target_payload)
        add = self._render_add_check(table, item.source_payload)
        return [
            self._stmt(item, drop),
            self._stmt(item, add,
                       down_sql=self._render_add_check(table, item.target_payload)),
        ]

    def _ri_check_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_check(item.parent_table, item.target_payload))]

    def _ri_index_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, ix = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_create_index(table, ix),
                           down_sql=self._render_drop_index(table, ix), down_confirmed=True)]

    def _ri_index_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        drop = self._render_drop_index(table, item.target_payload)
        create = self._render_create_index(table, item.source_payload)
        return [
            self._stmt(item, drop),
            self._stmt(item, create,
                       down_sql=self._render_create_index(table, item.target_payload)),
        ]

    def _ri_index_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, ix = item.parent_table, item.target_payload
        return [self._stmt(item, self._render_drop_index(table, ix),
                           down_sql=self._render_create_index(table, ix), down_confirmed=False)]

    def _ri_column_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        fwd = self._render_modify_column(table, item.source_payload, item.target_payload,
                                         item.changed_attributes)
        rev = self._render_modify_column(table, item.target_payload, item.source_payload,
                                         item.changed_attributes)
        rev_sql = ";\n".join(rev) if rev else None
        return [self._stmt(item, s, down_sql=rev_sql, down_confirmed=False) for s in fwd]

    def _ri_pk_changed(self, item: DiffItem) -> list[RenderedStatement]:
        # Cubre new/modified/dropped: _render_alter_pk decide DROP/ADD/ambos según payloads.
        table = item.parent_table
        return [self._stmt(item, s) for s in self._render_alter_pk(table, item.source_payload, item.target_payload)]

    def _ri_view_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        return [self._stmt(item, s) for s in self._render_view(item.source_payload, replace)]

    def _ri_view_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_view(item.target_payload))]

    def _ri_routine_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        return [self._stmt(item, s) for s in self._render_routine(item.source_payload, replace)]

    def _ri_routine_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_routine(item.target_payload))]

    def _ri_trigger_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        return [self._stmt(item, s) for s in self._render_trigger(item.source_payload, replace)]

    def _ri_trigger_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_trigger(item.target_payload))]

    def _ri_event_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        return [self._stmt(item, s) for s in self._render_event(item.source_payload, replace)]

    def _ri_event_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_event(item.target_payload))]

    def _ri_sequence_new(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, s) for s in self._render_sequence(item.source_payload, alter=False)]

    def _ri_sequence_modified(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, s) for s in self._render_sequence(item.source_payload, alter=True)]

    def _ri_sequence_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_sequence(item.target_payload))]

    def _ri_enum_new(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, s) for s in self._render_enum(item.source_payload, item.target_payload)]

    def _ri_enum_modified(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, s) for s in self._render_enum(item.source_payload, item.target_payload)]

    def _ri_enum_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_enum(item.target_payload))]

    def _ri_extension_new(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_extension(item.source_payload))]

    def _ri_extension_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return [self._stmt(item, self._render_drop_extension(item.target_payload))]

    # ---- Renderer portable de FK (ambos motores comparten esta sintaxis) ---- #
    def _render_add_fk(self, table: str, fk) -> str:
        cols = ", ".join(self._q(c, "columna") for c in fk.columns)
        ref_cols = ", ".join(self._q(c, "columna") for c in fk.referred_columns)
        ref = self._q(fk.referred_table, "tabla")
        name = f"CONSTRAINT {self._q(fk.name, 'constraint')} " if fk.name else ""
        sql = (
            f"ALTER TABLE {self._q(table, 'tabla')} ADD {name}"
            f"FOREIGN KEY ({cols}) REFERENCES {ref} ({ref_cols})"
        )
        if fk.on_delete:
            sql += f" ON DELETE {self._sanitize_referential_action(fk.on_delete)}"
        if fk.on_update:
            sql += f" ON UPDATE {self._sanitize_referential_action(fk.on_update)}"
        return sql

    @staticmethod
    def _sanitize_referential_action(action: str) -> str:
        """Whitelist de acciones referenciales (nunca interpola texto crudo del motor)."""
        allowed = {"CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"}
        norm = (action or "").strip().upper()
        if norm not in allowed:
            raise AppHttpException(
                message="Acción referencial de FK no reconocida.",
                status_code=422, context={"action": norm},
            )
        return norm

    # ------------------------------------------------------------------ #
    # Hooks de rendering específicos de dialecto                           #
    # ------------------------------------------------------------------ #
    # NO son @abstractmethod a propósito: un ServerAdapter puede existir solo para
    # introspección (p.ej. dobles de test) sin capacidad de rendering. Los adapters
    # reales (MySQL/MariaDB/PostgreSQL) los implementan todos; llamar a uno no
    # implementado falla ruidosamente (nunca genera DDL silenciosamente incorrecto).
    def _render_column_def(self, col) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_column_def")

    def _render_create_table(self, tbl) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_create_table")

    def _render_modify_column(self, table, src_col, tgt_col, changed: list[str]) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_modify_column")

    def _render_drop_fk(self, table: str, fk) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_fk")

    def _render_drop_unique(self, table: str, uc) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_unique")

    def _render_drop_check(self, table: str, ck) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_check")

    def _render_create_index(self, table: str, ix) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_create_index")

    def _render_drop_index(self, table: str, ix) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_index")

    def _render_alter_pk(self, table: str, src_tbl, tgt_tbl) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_alter_pk")

    def _render_view(self, view, replace: bool) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_view")

    def _render_drop_view(self, view) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_view")

    def _render_routine(self, routine, replace: bool) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_routine")

    def _render_drop_routine(self, routine) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_routine")

    def _render_trigger(self, trigger, replace: bool) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_trigger")

    def _render_drop_trigger(self, trigger) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_trigger")

    # Los siguientes solo aplican a un motor; el default degrada a no-op para el otro.
    def _render_event(self, event, replace: bool) -> list[str]:
        return []

    def _render_drop_event(self, event) -> str:
        return f"DROP EVENT {self._q(event.name, 'event')}"

    def _render_sequence(self, seq, *, alter: bool) -> list[str]:
        return []

    def _render_drop_sequence(self, seq) -> str:
        return f"DROP SEQUENCE {self._q(seq.name, 'secuencia')}"

    def _render_enum(self, src_enum, tgt_enum) -> list[str]:
        return []

    def _render_drop_enum(self, enum) -> str:
        return f"DROP TYPE {self._q(enum.name, 'tipo')}"

    def _render_extension(self, ext) -> str:
        return f"CREATE EXTENSION IF NOT EXISTS {self._q(ext.name, 'extension')}"

    def _render_drop_extension(self, ext) -> str:
        return f"DROP EXTENSION {self._q(ext.name, 'extension')}"

    # ------------------------------------------------------------------ #
    # Datos-semilla (snapshot selectivo) — read-only, cross-dialect       #
    # ------------------------------------------------------------------ #
    def list_table_stats(self, database: str) -> list[TableStat]:
        """
        Estimación por tabla (filas + tiene PK) para informar la selección de datos.
        Solo métricas del catálogo, NUNCA valores de filas.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                insp = inspect(conn)
                out: list[TableStat] = []
                for t in sorted(insp.get_table_names(schema=schema)):
                    pk = (
                        insp.get_pk_constraint(t, schema=schema).get("constrained_columns")
                        or []
                    )
                    out.append(
                        TableStat(
                            table=t,
                            estimated_rows=self._estimate_rows(conn, t, schema),
                            has_primary_key=bool(pk),
                        )
                    )
                return out
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_table_stats", target=self.target, extra={"database": database}
            )

    def dump_table_data(
        self,
        database: str,
        table: str,
        *,
        mode: str = "upsert",
        max_rows: int,
        max_bytes: int,
        batch_rows: int,
    ) -> SeedResult:
        """
        Extrae datos-semilla de UNA tabla como INSERT idempotente + rollback por PK.

        Fail-closed: sin PK, sin filas o si supera los guardrails (filas/bytes) → se
        OMITE (``included=False`` + ``reason``), nunca se emite SQL parcial. Los topes
        se acotan además por los techos duros de ``snapshot_data``.
        """
        max_rows, max_bytes = snapshot_data.effective_limits(max_rows, max_bytes)
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        validate_identifier(table, self.dialect, "tabla", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                insp = inspect(conn)
                pk = (
                    insp.get_pk_constraint(table, schema=schema).get("constrained_columns")
                    or []
                )
                if not pk:
                    return SeedResult(table=table, included=False, reason="no_primary_key")
                tbl = Table(table, MetaData(), autoload_with=conn, schema=schema)
                columns = [c.name for c in tbl.columns]
                # Defensa en dos capas: validar (no solo quotear) los identificadores
                # reflejados. Un nombre anómalo omite la tabla (fail-closed).
                try:
                    for c in columns:
                        validate_identifier(c, self.dialect, "columna", allow_existing=True)
                except AppHttpException:
                    return SeedResult(table=table, included=False, reason="invalid_identifier")
                order_cols = [tbl.c[c] for c in pk]
                # Streaming (yield_per) + LIMIT max_rows+1: acota la memoria (no materializa
                # filas grandes antes del guard de bytes) y detecta "supera el máximo".
                result = conn.execution_options(
                    stream_results=True, yield_per=max(1, batch_rows)
                ).execute(select(tbl).order_by(*order_cols).limit(max_rows + 1))
                return snapshot_data.build_seed(
                    dialect=self.dialect, table=table, columns=columns, pk=pk,
                    rows=result, mode=mode, batch_rows=batch_rows,
                    max_rows=max_rows, max_bytes=max_bytes,
                )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="dump_table_data", target=self.target,
                extra={"database": database, "table": table},
            )

    # ------------------------------------------------------------------ #
    # Helpers para DDL/DCL (usados por las operaciones de escritura)      #
    # ------------------------------------------------------------------ #
    def _execute_server(
        self, statements: list[str], *, op: str, extra: dict | None = None
    ) -> None:
        """Ejecuta sentencias a NIVEL SERVIDOR (AUTOCOMMIT). Para DDL/DCL."""
        try:
            with server_connection(self.target) as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op=op, target=self.target, extra=extra)

    def _execute_database(
        self, database: str, statements: list[str], *, op: str, extra: dict | None = None
    ) -> None:
        """Ejecuta sentencias conectado a una BD CONCRETA (grants schema-level PG)."""
        try:
            with database_connection(self.target, database) as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                for stmt in statements:
                    conn.execute(text(stmt))
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op=op, target=self.target, extra=extra)
