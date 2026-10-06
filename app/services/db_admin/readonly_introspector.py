"""
El façade de SOLO LECTURA que usa el MCP para leer el catálogo de una base (plan 12 §5.3).

COMPOSICIÓN, NO HERENCIA, Y EL MOTIVO VA ESCRITO
------------------------------------------------
``ServerAdapter`` expone ``create_user``, ``drop_database``, ``grant_*`` y ``render_diff``.
Heredar y sobreescribir-para-lanzar es una **blocklist**: cada método nuevo del adapter nacería
alcanzable desde una tool y habría que acordarse de taparlo. Componer es una **allowlist**: el
façade tiene exactamente los métodos que declara, y agregar un método mutante al adapter no
agrega nada acá. Por eso el adapter vive en un atributo con *name mangling* y no hay
``__getattr__``: el façade no lo reexpone.

LO QUE GARANTIZA LA SESIÓN
--------------------------
Todo corre dentro de ``export_session(..., snapshot_data=False)``: ``READ ONLY`` en la sesión,
timeouts propios del MCP (``MCP_SESSION_MAX_SECONDS``, ``MCP_STATEMENT_TIMEOUT_MS``) y el
``rollback`` + cierre garantizados en un ``finally``. La credencial es la de solo lectura del
servidor: quien arma el ``target`` es ``target_resolution.open_readonly``, que nunca lee la
pseudo-root.

LOS CUERPOS NO SALEN DE ACÁ COMO CONTRATO, PERO SÍ VIAJAN EN LOS DTO INTERNOS
----------------------------------------------------------------------------
Los hooks del snapshot devuelven vistas, rutinas y triggers con su cuerpo. El façade los entrega
tal cual porque son DTOs internos; quien decide qué sale al agente es el mapeador de
``app/mcp/tools/catalog.py``, que construye cada salida campo por campo. Esta capa garantiza
solo lectura; la otra, lista blanca.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from app.core.environments import MCP_SESSION_MAX_SECONDS, MCP_STATEMENT_TIMEOUT_MS
from app.core.remote_engine import ServerTarget
from app.services.db_admin.dtos import (
    DefinitionRead,
    EventInfo,
    RoutineInfo,
    SchemaSnapshot,
    SequenceInfo,
    TableSchema,
    TriggerInfo,
    ViewInfo,
)
from app.services.db_admin.export_session import ExportSession, export_session


class ReadonlyIntrospector:
    """
    Los métodos que una tool del MCP puede llamar, y ninguno más.

    No se instancia a mano: lo produce ``readonly_introspection``, que es quien abre y cierra la
    sesión de lectura.
    """

    __slots__ = ("__adapter", "__session", "database")

    def __init__(self, adapter, session: ExportSession, database: str):
        self.__adapter = adapter
        self.__session = session
        self.database = database

    @property
    def warnings(self) -> list[str]:
        """Directivas de sesión que el motor rechazó. Se reportan, nunca se tapan."""
        return list(self.__session.degradations)

    @property
    def consistent_structure(self) -> bool:
        return self.__session.supports_consistent_structure

    def object_index(self) -> dict[str, list[str]]:
        """``{kind: [nombres]}``. Ver ``ServerAdapter.list_object_names``."""
        self.__session.check_deadline()
        return self.__adapter.list_object_names(self.database, conn=self.__session.conn)

    def table_schemas(self, tables: Sequence[str]) -> list[TableSchema]:
        """El detalle de cada tabla pedida, dentro de la misma sesión de lectura."""
        out: list[TableSchema] = []
        for name in tables:
            self.__session.check_deadline()
            out.append(
                self.__adapter.get_table_schema(self.database, name, conn=self.__session.conn)
            )
        return out

    def _schema(self) -> str:
        return self.__adapter._inspect_schema(self.database)

    def views(self) -> list[ViewInfo]:
        self.__session.check_deadline()
        return self.__adapter._snapshot_views(self.__session.conn, self.database, self._schema())

    def routines(self) -> list[RoutineInfo]:
        self.__session.check_deadline()
        return self.__adapter._snapshot_routines(self.__session.conn, self.database, self._schema())

    def triggers(self) -> list[TriggerInfo]:
        self.__session.check_deadline()
        return self.__adapter._snapshot_triggers(self.__session.conn, self.database, self._schema())

    def events(self) -> list[EventInfo]:
        """
        Events del scheduler con su programación. ``[]`` en motores sin scheduler (PostgreSQL).

        Como ``views``/``routines``/``triggers``, entrega el cuerpo en el DTO interno: el
        mapeador de la tool decide qué sale al agente.
        """
        self.__session.check_deadline()
        return self.__adapter._snapshot_events(self.__session.conn, self.database, self._schema())

    def definition(
        self, kind: str, name: str, routine_kind: str | None = None
    ) -> list[DefinitionRead]:
        """
        Código de UN objeto del índice (``view``/``trigger``/``event``/``routine``).

        Devuelve una lectura por sobrecarga o tipo de rutina (ver ``ServerAdapter.read_definition``).
        El cuerpo llega SIN redactar: ``definition_reader.build_definition`` redacta, mide y
        huella. Corre en la misma sesión de solo lectura que el resto de la fachada.
        """
        self.__session.check_deadline()
        return self.__adapter.read_definition(
            self.__session.conn, self.database, self._schema(), kind, name, routine_kind
        )

    def server_version(self) -> str | None:
        """Versión del servidor (``VERSION()``), o ``None`` si no se pudo leer."""
        self.__session.check_deadline()
        return self.__adapter.read_server_version(self.__session.conn)

    def sequences(self) -> list[SequenceInfo]:
        self.__session.check_deadline()
        return self.__adapter._snapshot_sequences(
            self.__session.conn, self.database, self._schema()
        )

    def column_counts(self, tables: Sequence[str]) -> dict[str, int]:
        """Cantidad de columnas por tabla. Ver ``ServerAdapter.column_counts``."""
        self.__session.check_deadline()
        return self.__adapter.column_counts(self.database, list(tables), conn=self.__session.conn)

    def applied_version(self, slug: str | None) -> str | None:
        """
        La versión que la tabla de Alembic de la base declara, o ``None`` si no hay tabla.

        Es **un SELECT y nada más** (plan 12 §6.2): no usa ``MigrationContext``, que es un internal
        privado de Alembic y cuyo vecino ``stamp()`` crea la tabla de versión — en MySQL ese
        ``CREATE TABLE`` es commit implícito e irreversible. Acá se resuelve el nombre real
        (vigente o histórico) con el Inspector, se verifica que exista y se lee con el
        identificador cuoteado por SQLAlchemy. Una base sin tabla devuelve ``None`` y no se crea
        nada; la sesión además está en ``READ ONLY``.
        """
        if not slug:
            return None
        from sqlalchemy import column, inspect, select, table

        from app.services.db_admin.migrations import resolve_version_table

        self.__session.check_deadline()
        conn = self.__session.conn
        schema = self._schema()
        nombre = resolve_version_table(conn, slug, schema)
        if not inspect(conn).has_table(nombre, schema=schema):
            return None
        t = table(nombre, column("version_num"), schema=schema)
        valor = conn.execute(select(t.c.version_num).limit(1)).scalar()
        return str(valor) if valor is not None else None

    def snapshot(self) -> SchemaSnapshot:
        """El snapshot estructural completo (entrada del diff). Solo estructura, jamás filas."""
        self.__session.check_deadline()
        return self.__adapter.structural_snapshot(self.database, conn=self.__session.conn)


@contextmanager
def readonly_introspection(target: ServerTarget, database: str) -> Iterator[ReadonlyIntrospector]:
    """
    Abre la sesión de lectura del MCP sobre ``database`` y rinde el façade. La cierra SIEMPRE.

    ``target`` tiene que llevar la credencial de SOLO LECTURA: este módulo no puede verificarlo
    (un ``ServerTarget`` no dice qué credencial lleva), así que la garantía la da su único
    llamador, ``target_resolution.open_readonly``.
    """
    from app.services.db_admin.factory import get_adapter

    adapter = get_adapter(target)
    with export_session(
        target,
        database,
        engine=adapter.dialect,
        max_duration_seconds=MCP_SESSION_MAX_SECONDS,
        statement_timeout_ms=MCP_STATEMENT_TIMEOUT_MS,
        idle_timeout_ms=MCP_STATEMENT_TIMEOUT_MS,
        snapshot_data=False,
    ) as session:
        yield ReadonlyIntrospector(adapter, session, database)


__all__ = ["ReadonlyIntrospector", "readonly_introspection"]
