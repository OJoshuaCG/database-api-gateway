"""
Controller de gestión de bases de datos a NIVEL SERVIDOR (patrón "por identidad", como
``ServerUserController``): crear, borrar y listar usuarios con permisos sobre una BD, sin
exigir que la BD esté adoptada en el inventario.

Operaciones destructivas (DROP): confirmación de DOBLE factor de backend — nombre exacto
(``confirm_target_name``) + token firmado con TTL (``confirm_token``, ver
``app.services.confirm_token``) — más guard de BDs de sistema (409) y auditoría fail-closed
(``audit.record_intent``) ANTES de tocar el motor. Cuando la BD está en el inventario, el DROP
también elimina su fila ``ManagedDatabase`` (para no dejar referencias colgadas).

Compatibilidad cross-engine: toda la lógica de motor vive en el adapter (MySQL/MariaDB y
PostgreSQL); este controller solo orquesta.
"""

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.controllers.managed_database_controller import ManagedDatabaseController
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.managed_database import ManagedDatabase
from app.models.server_user import ServerUser
from app.schemas.server_database import (
    DatabaseCreateOut,
    DatabaseDropOut,
    DatabaseGranteeOut,
    DatabaseGranteesOut,
    DropPreviewOut,
)
from app.services import audit, charset_catalog, confirm_token
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import (
    ensure_not_reserved_database,
    validate_identifier,
)

_DROP_OP = "drop-db"


class ServerDatabaseController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Crear                                                               #
    # ------------------------------------------------------------------ #
    def create_database(
        self,
        server_id: int,
        *,
        name: str,
        charset: str | None = None,
        collation: str | None = None,
        owner: str | None = None,
        register: bool = False,
        owner_id: int | None = None,
        notes: str | None = None,
        admin: dict | None = None,
    ) -> DatabaseCreateOut:
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            target = build_target(server)
        finally:
            session.close()

        # El nombre lo CREA el gateway → whitelist estricta + guard de sistema.
        validate_identifier(name, dialect, "base de datos")
        ensure_not_reserved_database(name, dialect)

        # Catálogo GLOBAL de charsets/collations: la combinación debe estar HABILITADA antes
        # de tocar el motor (422 si no). Se reemplazan los valores por la forma CANÓNICA del
        # catálogo: lo que viaja al DDL sale siempre de la tabla, nunca del texto crudo del
        # request (importa sobre todo en PostgreSQL, donde el locale va como literal de
        # string y no como identificador whitelisteado). Si el caller no eligió NADA, no se
        # valida: el adapter aplica su default, que corresponde a la fila is_default del seed.
        charset, collation = charset_catalog.resolve_enabled_combination(
            dialect, charset, collation
        )

        if register:
            if owner_id is None:
                raise AppHttpException(
                    message="Para registrar la BD en el inventario se requiere 'owner_id'.",
                    status_code=422,
                    context={"required": "owner_id"},
                )
            # Reutiliza el flujo existente: crea en el motor Y persiste el inventario de
            # forma consistente (status pending→active/error, auditoría propia).
            result = ManagedDatabaseController().create_database(
                {
                    "server_id": server_id,
                    "name": name,
                    "owner_id": owner_id,
                    "charset": charset,
                    "collation": collation,
                    "notes": notes,
                },
                provision=True,
                admin=admin,
            )
            return DatabaseCreateOut(
                database=name, engine=dialect, registered=True,
                managed_database_id=result["id"],
            )

        # Create-only ("así de la nada"): no toca el inventario.
        audit.record_intent(
            "server_database.create",
            admin=admin,
            target_type="server_database",
            target_id=None,
            server_id=server_id,
            touched_engine=True,
            detail=f"CREATE DATABASE {name}",
        )
        get_adapter(target).create_database(
            name, charset=charset, collation=collation, owner=owner
        )
        audit.record(
            "server_database.create",
            admin=admin,
            target_type="server_database",
            target_id=None,
            server_id=server_id,
            touched_engine=True,
        )
        return DatabaseCreateOut(database=name, engine=dialect, registered=False)

    # ------------------------------------------------------------------ #
    # Borrar — preview + delete                                           #
    # ------------------------------------------------------------------ #
    def _load_context(self, server_id: int, database: str):
        """(dialect, target, managed_row_id) — cierra la sesión antes de tocar el motor."""
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            target = build_target(server)
            managed = (
                session.query(ManagedDatabase)
                .filter(
                    ManagedDatabase.server_id == server_id,
                    ManagedDatabase.name == database,
                )
                .first()
            )
            managed_id = managed.id if managed else None
        finally:
            session.close()
        return dialect, target, managed_id

    def drop_preview(
        self, server_id: int, database: str, *, admin: dict | None = None
    ) -> DropPreviewOut:
        dialect, target, managed_id = self._load_context(server_id, database)
        validate_identifier(database, dialect, "base de datos", allow_existing=True)
        ensure_not_reserved_database(database, dialect)

        adapter = get_adapter(target)
        if database not in adapter.list_databases():
            raise AppHttpException(
                message="La base de datos no existe en el servidor.",
                status_code=404,
                context={"database": database},
            )
        active = adapter.active_connections(database)
        warnings: list[str] = []
        if active > 0:
            warnings.append(
                f"La base de datos tiene {active} conexión(es) activa(s); usa "
                "force_disconnect=true para terminarlas antes del DROP "
                "(obligatorio en PostgreSQL si hay conexiones)."
            )
        if managed_id is not None:
            warnings.append(
                "La base de datos está registrada en el inventario; al eliminarla también "
                "se borrará su registro gestionado."
            )
        token, expires_at = confirm_token.issue(_DROP_OP, server_id, database)
        return DropPreviewOut(
            database=database,
            engine=dialect,
            active_connections=active,
            is_managed=managed_id is not None,
            managed_database_id=managed_id,
            confirm_token=token,
            expires_at=expires_at,
            warnings=warnings,
        )

    def drop_database(
        self,
        server_id: int,
        database: str,
        *,
        confirm_target_name: str,
        confirm_token_value: str,
        force_disconnect: bool = False,
        admin: dict | None = None,
    ) -> DatabaseDropOut:
        dialect, target, managed_id = self._load_context(server_id, database)
        validate_identifier(database, dialect, "base de datos", allow_existing=True)
        ensure_not_reserved_database(database, dialect)

        # Confirmación de DOBLE factor de backend: nombre exacto + token firmado/vigente.
        if confirm_target_name != database:
            raise AppHttpException(
                message="confirm_target_name no coincide con el nombre de la base de datos.",
                status_code=422,
                context={"required": "confirm_target_name == database"},
            )
        confirm_token.verify(confirm_token_value, _DROP_OP, server_id, database)

        adapter = get_adapter(target)
        # Conteo informativo previo (solo cuando vamos a terminar conexiones).
        terminated = adapter.active_connections(database) if force_disconnect else 0

        audit.record_intent(
            "server_database.drop",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            touched_engine=True,
            detail=f"DROP DATABASE {database} (force_disconnect={force_disconnect})",
        )
        adapter.drop_database(database, force_disconnect=force_disconnect)

        inventory_removed = False
        if managed_id is not None:
            session = self._session()
            try:
                md = session.get(ManagedDatabase, managed_id)
                if md:
                    session.delete(md)
                    session.commit()
                    inventory_removed = True
            finally:
                session.close()

        audit.record(
            "server_database.drop",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            touched_engine=True,
        )
        return DatabaseDropOut(
            database=database,
            engine=dialect,
            dropped=True,
            inventory_removed=inventory_removed,
            terminated_connections=terminated,
        )

    # ------------------------------------------------------------------ #
    # Usuarios/roles con permisos sobre la BD                             #
    # ------------------------------------------------------------------ #
    def list_database_grantees(
        self, server_id: int, database: str, *, admin: dict | None = None
    ) -> DatabaseGranteesOut:
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            target = build_target(server)
            inv = [
                (u.id, u.username, u.host)
                for u in session.query(ServerUser)
                .filter(ServerUser.server_id == server_id)
                .all()
            ]
        finally:
            session.close()

        validate_identifier(database, dialect, "base de datos", allow_existing=True)
        adapter = get_adapter(target)
        if database not in adapter.list_databases():
            raise AppHttpException(
                message="La base de datos no existe en el servidor.",
                status_code=404,
                context={"database": database},
            )

        is_pg = dialect == "postgresql"
        supports_hosts = getattr(adapter, "supports_hosts", not is_pg)
        grantees = adapter.list_database_grantees(database)

        def key(username: str, host: str | None) -> tuple:
            return (username,) if is_pg else (username, host or "%")

        inv_by_key = {key(r[1], r[2]): r for r in inv}

        out = []
        for g in grantees:
            row = inv_by_key.get(key(g.username, g.host))
            out.append(
                DatabaseGranteeOut(
                    username=g.username,
                    host=None if is_pg else (g.host or "%"),
                    is_global=g.is_global,
                    privileges=g.privileges,
                    levels=g.levels,
                    status="adopted" if row else "unmanaged",
                    server_user_id=row[0] if row else None,
                )
            )
        out.sort(key=lambda x: (x.username, x.host or ""))
        return DatabaseGranteesOut(
            dialect=dialect, supports_hosts=supports_hosts, database=database, grantees=out
        )
