"""
Helpers compartidos por los controllers que operan sobre servidores destino.

Centraliza el armado del ``ServerTarget`` (descifrando la credencial en memoria) y
la carga del ``Server`` con 404, para no duplicar esa lógica en cada controller.

La credencial descifrada vive solo en el ``ServerTarget`` el tiempo mínimo para
abrir la conexión; nunca se persiste, serializa ni loguea.
"""

from app.core.crypto import CryptoConfigError, CryptoError, decrypt
from app.core.environments import REMOTE_SSL_MODE
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.models.enums import EngineType
from app.models.server import Server


def engine_value(server: Server) -> str:
    """Devuelve el dialecto como string ('mysql' | 'mariadb' | 'postgresql')."""
    return (
        server.engine.value
        if isinstance(server.engine, EngineType)
        else str(server.engine)
    )


def get_server_or_404(session, server_id: int) -> Server:
    server = session.get(Server, server_id)
    if not server:
        raise AppHttpException(
            message="Servidor no encontrado.",
            status_code=404,
            context={"server_id": server_id},
        )
    return server


def build_target(server: Server) -> ServerTarget:
    """Arma el ServerTarget descifrando la credencial pseudo-root en memoria."""
    try:
        password = decrypt(server.root_password_encrypted)
    except (CryptoError, CryptoConfigError) as exc:
        raise AppHttpException(
            message="No se pudo descifrar la credencial del servidor.",
            status_code=500,
            context={"server_id": server.id},
        ) from exc
    return ServerTarget(
        server_id=server.id,
        dialect=engine_value(server),
        host=server.host,
        port=server.port,
        admin_user=server.root_username,
        admin_password=password,
        ssl_mode=REMOTE_SSL_MODE,
    )
