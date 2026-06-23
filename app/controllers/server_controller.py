"""
Controller de Servers.

- CRUD del inventario sobre la BD de metadatos del gateway (ORM SQLAlchemy).
- Cifra/descifra la credencial pseudo-root con `app.core.crypto`.
- Para test-connection e introspección, arma un `ServerTarget` (descifrando en
  memoria) y delega en el `ServerAdapter` correspondiente.

La credencial descifrada NUNCA se persiste, se serializa ni se loguea.
"""

from sqlalchemy.exc import IntegrityError

from app.core.crypto import CryptoConfigError, CryptoError, decrypt, encrypt
from app.core.database import Database
from app.core.net_guard import validate_remote_host
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    REMOTE_SSL_MODE,
)
from app.core import remote_engine
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.models.enums import EngineType, ServerStatus
from app.models.server import Server
from app.services.db_admin.dtos import ConnectionInfo, EngineUserInfo, TableSchema
from app.services.db_admin.factory import get_adapter


class ServerController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(s: Server) -> dict:
        """Dict seguro para la API: SIN la credencial cifrada."""
        return {
            "id": s.id,
            "name": s.name,
            "host": s.host,
            "port": s.port,
            "engine": s.engine,
            "root_username": s.root_username,
            "ssl_mode": s.ssl_mode,
            "status": s.status,
            "is_active": s.is_active,
            "notes": s.notes,
            "has_root_password": bool(s.root_password_encrypted),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }

    @staticmethod
    def _encrypt_password(plaintext: str) -> str:
        try:
            return encrypt(plaintext)
        except (CryptoError, CryptoConfigError) as exc:
            raise AppHttpException(
                message="No se pudo cifrar la credencial del servidor.",
                status_code=500,
            ) from exc

    def _get_or_404(self, session, server_id: int) -> Server:
        server = session.get(Server, server_id)
        if not server:
            raise AppHttpException(
                message="Servidor no encontrado.",
                status_code=404,
                context={"server_id": server_id},
            )
        return server

    def _set_status(self, server_id: int, status: ServerStatus) -> None:
        session = self._session()
        try:
            server = session.get(Server, server_id)
            if server:
                server.status = status
                session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # CRUD (solo BD del gateway)                                          #
    # ------------------------------------------------------------------ #
    def list_servers(self, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            total = session.query(Server).count()
            rows = (
                session.query(Server)
                .order_by(Server.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize(s) for s in rows], total
        finally:
            session.close()

    def get_server(self, server_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, server_id))
        finally:
            session.close()

    def create_server(self, data: dict) -> dict:
        # Anti-SSRF: validar el destino ANTES de persistir/conectar.
        validate_remote_host(data["host"])
        session = self._session()
        try:
            server = Server(
                name=data["name"],
                host=data["host"],
                port=data["port"],
                engine=EngineType(data["engine"]),
                root_username=data["root_username"],
                root_password_encrypted=self._encrypt_password(data["root_password"]),
                ssl_mode=data.get("ssl_mode"),
                notes=data.get("notes"),
                is_active=data.get("is_active", True),
            )
            session.add(server)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un servidor con ese nombre o host:puerto.",
                    status_code=409,
                    context={"name": data.get("name")},
                ) from exc
            session.refresh(server)
            return self._serialize(server)
        finally:
            session.close()

    def update_server(self, server_id: int, data: dict) -> dict:
        # Anti-SSRF: si cambia el host, validar el nuevo destino.
        if data.get("host") is not None:
            validate_remote_host(data["host"])
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            for field in ("name", "host", "port", "notes", "is_active", "root_username", "ssl_mode"):
                if field in data:
                    setattr(server, field, data[field])
            if data.get("engine") is not None:
                server.engine = EngineType(data["engine"])
            if data.get("root_password"):
                server.root_password_encrypted = self._encrypt_password(
                    data["root_password"]
                )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un servidor con ese nombre o host:puerto.",
                    status_code=409,
                    context={"server_id": server_id},
                ) from exc
            session.refresh(server)
            result = self._serialize(server)
        finally:
            session.close()
        # Datos de conexión pudieron cambiar: descartar engines remotos cacheados.
        remote_engine.invalidate_server(server_id)
        return result

    def delete_server(self, server_id: int) -> None:
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            session.delete(server)
            session.commit()
        finally:
            session.close()
        remote_engine.invalidate_server(server_id)

    # ------------------------------------------------------------------ #
    # Operaciones contra el servidor destino                              #
    # ------------------------------------------------------------------ #
    def _build_target(self, server_id: int) -> ServerTarget:
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            engine_value = (
                server.engine.value
                if isinstance(server.engine, EngineType)
                else str(server.engine)
            )
            try:
                password = decrypt(server.root_password_encrypted)
            except (CryptoError, CryptoConfigError) as exc:
                raise AppHttpException(
                    message="No se pudo descifrar la credencial del servidor.",
                    status_code=500,
                    context={"server_id": server_id},
                ) from exc
            return ServerTarget(
                server_id=server.id,
                dialect=engine_value,
                host=server.host,
                port=server.port,
                admin_user=server.root_username,
                admin_password=password,
                # TLS por conexión: el del servidor manda; si no tiene, cae al global.
                ssl_mode=server.ssl_mode if server.ssl_mode is not None else REMOTE_SSL_MODE,
            )
        finally:
            session.close()

    def test_connection(self, server_id: int) -> ConnectionInfo:
        adapter = get_adapter(self._build_target(server_id))
        try:
            info = adapter.test_connection()
        except AppHttpException:
            self._set_status(server_id, ServerStatus.unreachable)
            raise
        self._set_status(server_id, ServerStatus.active)
        return info

    def list_databases(self, server_id: int) -> list[str]:
        return get_adapter(self._build_target(server_id)).list_databases()

    def list_users(self, server_id: int) -> list[EngineUserInfo]:
        return get_adapter(self._build_target(server_id)).list_users()

    def list_tables(self, server_id: int, database: str) -> list[str]:
        return get_adapter(self._build_target(server_id)).list_tables(database)

    def get_table_schema(
        self, server_id: int, database: str, table: str
    ) -> TableSchema:
        return get_adapter(self._build_target(server_id)).get_table_schema(
            database, table
        )
