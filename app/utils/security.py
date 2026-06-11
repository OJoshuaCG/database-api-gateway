"""
Hashing de contraseñas con Argon2 (argon2-cffi).

Argon2id es el algoritmo recomendado por OWASP para almacenamiento de passwords.
Se usa para el password del administrador del gateway.
"""

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Devuelve el hash Argon2id de un password en texto plano."""
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verifica un password contra su hash. Devuelve False ante cualquier fallo."""
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
