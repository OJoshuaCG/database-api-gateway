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


#: Largo mínimo de una password que elige una persona.
#:
#: Vive acá y no en el schema ni en el controller porque los DOS la necesitan y una constante
#: duplicada es una constante que se desincroniza: el día que alguien suba una y no la otra, el
#: schema rechaza lo que el controller acepta (o peor, al revés).
#:
#: Y no hay política de composición a propósito: las reglas de "una mayúscula y un símbolo"
#: empujan a `Password1!` y bajan la entropía real. Largo mínimo alto y nada más.
PASSWORD_MIN_LENGTH = 12


def hash_password(password: str) -> str:
    """Devuelve el hash Argon2id de un password en texto plano."""
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verifica un password contra su hash. Devuelve False ante cualquier fallo."""
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
