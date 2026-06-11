"""
Cifrado simétrico de secretos del inventario.

Deriva una clave Fernet determinística desde SECRET_KEY usando HKDF-SHA256 y
expone encrypt/decrypt sobre strings. Se usa para las credenciales pseudo-root
de los servidores destino (y, a futuro, passwords de usuarios del motor).
Nunca se persiste texto plano ni se loguea el contenido descifrado.

Diseño:
- La clave NO se evalúa al import (lazy + lru_cache) para no romper procesos que
  no cifran nada, como las migraciones de Alembic.
- Este módulo es infraestructura pura: lanza excepciones propias
  (CryptoConfigError / CryptoError), NO AppHttpException. Los controllers son
  responsables de traducir a respuestas HTTP.
"""

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.environments import CRYPTO_KEY_SALT, SECRET_KEY

# Context binding / versionado de la derivación. Cambiarlo invalida los tokens
# existentes (útil para una futura rotación de clave con MultiFernet).
_HKDF_INFO = b"db-gateway-fernet-v1"


class CryptoConfigError(RuntimeError):
    """SECRET_KEY ausente u otra configuración de cifrado inválida."""


class CryptoError(RuntimeError):
    """Fallo al cifrar/descifrar (token corrupto, clave distinta, etc.)."""


def _derive_fernet_key(secret_key: str, salt: bytes) -> bytes:
    """
    Deriva 32 bytes desde SECRET_KEY con HKDF(SHA256) y los devuelve en
    base64 urlsafe, que es el formato exacto que exige Fernet.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    )
    raw_key = hkdf.derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw_key)


@lru_cache(maxsize=1)
def get_fernet() -> Fernet:
    """
    Construye (y cachea) la instancia Fernet a partir de SECRET_KEY.

    Raises:
        CryptoConfigError: si SECRET_KEY no está definido.
    """
    if not SECRET_KEY:
        raise CryptoConfigError(
            "SECRET_KEY no está definido; no se pueden cifrar/descifrar secretos."
        )
    key = _derive_fernet_key(SECRET_KEY, CRYPTO_KEY_SALT.encode("utf-8"))
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """
    Cifra un string y devuelve el token Fernet (str utf-8, urlsafe base64).

    Raises:
        CryptoConfigError: si SECRET_KEY no está definido.
        CryptoError:       si el valor no es un string no vacío.
    """
    if not isinstance(plaintext, str) or plaintext == "":
        raise CryptoError("Solo se puede cifrar un string no vacío.")
    token = get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(token: str) -> str:
    """
    Descifra un token Fernet y devuelve el texto plano.

    Raises:
        CryptoConfigError: si SECRET_KEY no está definido.
        CryptoError:       si el token está corrupto o fue cifrado con otra clave.
    """
    if not isinstance(token, str) or token == "":
        raise CryptoError("Token inválido para descifrar.")
    try:
        return get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # No incluir el token ni el plaintext en el mensaje.
        raise CryptoError("No se pudo descifrar el secreto (token inválido).") from exc


def try_decrypt(token: str | None) -> str | None:
    """
    Variante no-lanzante para listados/health-checks. Devuelve None si falla
    o si el token es None/vacío.
    """
    if not token:
        return None
    try:
        return decrypt(token)
    except (CryptoError, CryptoConfigError):
        return None
