"""
Rotación de la clave de cifrado (DEK) — envelope encryption.

Genera una DEK nueva, **re-cifra todas las credenciales** almacenadas (de la DEK
actual a la nueva) y marca la DEK nueva como activa, todo en UNA transacción.
`SECRET_KEY` (la KEK) NO cambia → no hay que tocar el `.env` ni reiniciar.

Columnas re-cifradas: `servers.root_password_encrypted` y
`server_users.password_encrypted`.

Limitación: si llega un cifrado concurrente justo durante la rotación podría quedar con
la DEK previa; ejecutar la rotación en una ventana de baja actividad. La operación es
atómica (rollback ante cualquier fallo de descifrado/escritura), así que nunca deja los
datos a medias.
"""

from app.core import crypto
from app.core.database import Database
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.crypto_key import CryptoKey
from app.models.server import Server
from app.models.server_user import ServerUser

logger = get_logger(__name__)


def rotate_data_key() -> dict:
    """Rota la DEK y re-cifra todas las credenciales. Devuelve un resumen de conteos."""
    old = crypto.current_data_key()
    new, new_wrapped = crypto.new_data_key()

    session = Database().get_declarative_base_session()
    try:
        servers_reencrypted = 0
        for server in session.query(Server).all():
            if server.root_password_encrypted:
                plaintext = old.decrypt(server.root_password_encrypted.encode("utf-8"))
                server.root_password_encrypted = new.encrypt(plaintext).decode("utf-8")
                servers_reencrypted += 1

        users_reencrypted = 0
        for user in (
            session.query(ServerUser)
            .filter(ServerUser.password_encrypted.isnot(None))
            .all()
        ):
            plaintext = old.decrypt(user.password_encrypted.encode("utf-8"))
            user.password_encrypted = new.encrypt(plaintext).decode("utf-8")
            users_reencrypted += 1

        # La DEK nueva pasa a ser la activa; las anteriores quedan inactivas.
        session.query(CryptoKey).filter(CryptoKey.is_active.is_(True)).update(
            {"is_active": False}
        )
        session.add(CryptoKey(dek_wrapped=new_wrapped, is_active=True))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise AppHttpException(
            message="No se pudo rotar la clave de cifrado; no se modificó ningún dato.",
            status_code=500,
        ) from exc
    finally:
        session.close()

    # El proceso debe usar la DEK nueva de inmediato.
    crypto.reset_dek_cache()
    logger.info(
        "Rotación de cifrado OK: %d servidores, %d usuarios re-cifrados.",
        servers_reencrypted,
        users_reencrypted,
    )
    return {
        "servers_reencrypted": servers_reencrypted,
        "server_users_reencrypted": users_reencrypted,
    }
