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
    IndexInfo,
    TableSchema,
)
from app.services.db_admin.identifiers import validate_identifier


class ServerAdapter(ABC):
    dialect: str

    def __init__(self, target: ServerTarget):
        self.target = target

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
        validate_identifier(database, self.dialect, "base de datos")
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                return sorted(inspect(conn).get_table_names(schema=schema))
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_tables", target=self.target, extra={"database": database}
            )

    def get_table_schema(self, database: str, table: str) -> TableSchema:
        validate_identifier(database, self.dialect, "base de datos")
        validate_identifier(table, self.dialect, "tabla")
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
