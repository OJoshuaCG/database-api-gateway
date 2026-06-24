"""
Cifrado simétrico de secretos del inventario, con **envelope encryption** (KEK/DEK).

Diseño:
- **KEK** (Key Encryption Key): se deriva de `SECRET_KEY` con HKDF-SHA256. NO cifra
  datos directamente: solo **envuelve** (cifra) la DEK.
- **DEK** (Data Encryption Key): clave Fernet que cifra los datos (credenciales). Se
  almacena en la tabla `crypto_keys`, envuelta por la KEK. Permite **rotar** la
  encriptación re-cifrando los datos SIN cambiar `SECRET_KEY` (ver
  `app/services/crypto_rotation.py`).
- **Fallback:** si no hay DEK activa en BD (sistema fresco, o procesos sin BD como las
  migraciones / tests puros), se usa la clave derivada de la KEK como DEK — idéntico al
  comportamiento previo a la introducción del envelope (retrocompatible).

`encrypt`/`decrypt` operan sobre la DEK activa. La rotación inserta una DEK nueva y
re-cifra los datos en una transacción; el cache en proceso se invalida con
`reset_dek_cache()`. Nunca se persiste texto plano ni se loguea el contenido descifrado.

Este módulo lanza excepciones propias (CryptoConfigError / CryptoError), NO
AppHttpException: los controllers/servicios traducen a respuestas HTTP.
"""

import base64
import threading
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.environments import CRYPTO_KEY_SALT, SECRET_KEY

# Context binding / versionado de la derivación de la KEK.
_HKDF_INFO = b"db-gateway-fernet-v1"


class CryptoConfigError(RuntimeError):
    """SECRET_KEY ausente u otra configuración de cifrado inválida."""


class CryptoError(RuntimeError):
    """Fallo al cifrar/descifrar (token corrupto, clave distinta, etc.)."""


def _derive_fernet_key(secret_key: str, salt: bytes) -> bytes:
    """Deriva 32 bytes desde SECRET_KEY con HKDF(SHA256) en base64 urlsafe (formato Fernet)."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_HKDF_INFO)
    raw_key = hkdf.derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw_key)


def _kek_key() -> bytes:
    if not SECRET_KEY:
        raise CryptoConfigError(
            "SECRET_KEY no está definido; no se pueden cifrar/descifrar secretos."
        )
    return _derive_fernet_key(SECRET_KEY, CRYPTO_KEY_SALT.encode("utf-8"))


@lru_cache(maxsize=1)
def _get_kek() -> Fernet:
    """KEK derivada de SECRET_KEY. Envuelve la DEK; no cifra datos directamente."""
    return Fernet(_kek_key())


# --------------------------------------------------------------------------- #
# DEK activa (cache en proceso, invalidable tras rotación)                     #
# --------------------------------------------------------------------------- #
_dek_lock = threading.Lock()
_dek_cache: Fernet | None = None


def _load_active_dek_key() -> bytes:
    """
    Clave DEK activa en claro (bytes). Si no hay fila activa (o no hay BD/tabla, p.ej.
    en tests puros de crypto o en migraciones), cae a la clave derivada de la KEK
    (comportamiento pre-envelope, retrocompatible).
    """
    try:
        from app.core.database import Database
        from app.models.crypto_key import CryptoKey

        session = Database().get_declarative_base_session()
        try:
            row = (
                session.query(CryptoKey)
                .filter(CryptoKey.is_active.is_(True))
                .order_by(CryptoKey.id.desc())
                .first()
            )
        finally:
            session.close()
        if row is not None:
            return _get_kek().decrypt(row.dek_wrapped.encode("utf-8"))
    except CryptoConfigError:
        raise
    except Exception:
        # Sin BD/tabla disponible: usar la KEK como DEK (pre-envelope).
        pass
    return _kek_key()


def _active_dek() -> Fernet:
    global _dek_cache
    with _dek_lock:
        if _dek_cache is None:
            _dek_cache = Fernet(_load_active_dek_key())
        return _dek_cache


def reset_dek_cache() -> None:
    """Invalida la DEK cacheada (tras rotar la clave de datos)."""
    global _dek_cache
    with _dek_lock:
        _dek_cache = None


# --------------------------------------------------------------------------- #
# API de cifrado                                                              #
# --------------------------------------------------------------------------- #
def encrypt(plaintext: str) -> str:
    """Cifra un string con la DEK activa y devuelve el token Fernet (str)."""
    if not isinstance(plaintext, str) or plaintext == "":
        raise CryptoError("Solo se puede cifrar un string no vacío.")
    return _active_dek().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Descifra un token Fernet con la DEK activa y devuelve el texto plano."""
    if not isinstance(token, str) or token == "":
        raise CryptoError("Token inválido para descifrar.")
    try:
        return _active_dek().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError("No se pudo descifrar el secreto (token inválido).") from exc


def try_decrypt(token: str | None) -> str | None:
    """Variante no-lanzante para listados/health-checks. None si falla o es vacío."""
    if not token:
        return None
    try:
        return decrypt(token)
    except (CryptoError, CryptoConfigError):
        return None


# --------------------------------------------------------------------------- #
# Primitivas para la rotación (usadas por app/services/crypto_rotation.py)     #
# --------------------------------------------------------------------------- #
def current_data_key() -> Fernet:
    """DEK activa actual (para descifrar lo existente durante una rotación)."""
    return _active_dek()


def new_data_key() -> tuple[Fernet, str]:
    """
    Genera una DEK nueva. Devuelve ``(fernet, dek_envuelta_para_almacenar)``: el Fernet
    para re-cifrar los datos y la DEK ya envuelta por la KEK para persistir en BD.
    """
    plaintext = Fernet.generate_key()
    wrapped = _get_kek().encrypt(plaintext).decode("utf-8")
    return Fernet(plaintext), wrapped
