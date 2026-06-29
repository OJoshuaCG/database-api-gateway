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

from sqlalchemy import inspect, text
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.core.remote_engine import (
    ServerTarget,
    database_connection,
    map_driver_error,
    server_connection,
)
from app.exceptions import AppHttpException
from app.services.db_admin.dtos import (
    ColumnInfo,
    ConnectionInfo,
    EngineUserInfo,
    ForeignKeyInfo,
    GrantInfo,
    GrantLevel,
    IndexInfo,
    ObjectRef,
    TableSchema,
)
from app.services.db_admin.identifiers import validate_identifier


class ServerAdapter(ABC):
    dialect: str

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
                    columns_raw = insp.get_columns(table, schema=schema)
                except NoSuchTableError:
                    raise AppHttpException(
                        message="La tabla no existe en la base de datos indicada.",
                        status_code=404,
                        context={"database": database, "table": table},
                    )
                pk_cols = (
                    insp.get_pk_constraint(table, schema=schema).get(
                        "constrained_columns"
                    )
                    or []
                )
                fks_raw = insp.get_foreign_keys(table, schema=schema)
                idx_raw = insp.get_indexes(table, schema=schema)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc,
                op="get_table_schema",
                target=self.target,
                extra={"database": database, "table": table},
            )

        pk_set = set(pk_cols)
        columns = [
            ColumnInfo(
                name=c["name"],
                type=str(c["type"]),
                nullable=bool(c.get("nullable", True)),
                default=None if c.get("default") is None else str(c.get("default")),
                primary_key=c["name"] in pk_set,
                autoincrement=c.get("autoincrement") in (True, "auto"),
                comment=c.get("comment"),
            )
            for c in columns_raw
        ]
        foreign_keys = [
            ForeignKeyInfo(
                name=fk.get("name"),
                columns=fk.get("constrained_columns") or [],
                referred_table=fk.get("referred_table") or "",
                referred_columns=fk.get("referred_columns") or [],
            )
            for fk in fks_raw
        ]
        indexes = [
            IndexInfo(
                name=ix.get("name"),
                columns=ix.get("column_names") or [],
                unique=bool(ix.get("unique")),
            )
            for ix in idx_raw
        ]
        return TableSchema(
            database=database,
            table=table,
            columns=columns,
            primary_key=list(pk_cols),
            foreign_keys=foreign_keys,
            indexes=indexes,
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
