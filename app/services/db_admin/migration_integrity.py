"""
Integridad y ordenamiento de versiones de migraciones de blueprint.

Módulo compartido (sin dependencias de controllers) para evitar acoplamiento
controller→controller. Contiene:

- ``compute_checksum``: hash de TODO el material ejecutable + identidad estructural
  (``version``). Detecta alteración directa de la fila en la BD del gateway antes de
  ejecutar nada en el motor o de usar ``version`` para construir rutas/identificadores.
- ``validate_version`` / ``VERSION_RE``: whitelist de versión (solo dígitos). Se
  re-valida en el runner antes de usar ``version`` en un path de filesystem o como
  identificador de revisión (defensa en profundidad anti path-traversal).
- ``version_sort_key``: clave NUMÉRICA de orden. El orden lexicográfico de strings de
  ancho variable es incorrecto ("9999" > "10000"); siempre comparar/ordenar por int.
"""

import hashlib
import re

from app.exceptions import AppHttpException

# Versión: solo dígitos, 4–10 (padding). Debe coincidir con el schema Pydantic.
VERSION_RE = re.compile(r"^\d{4,10}$")

# Separador entre campos para evitar colisiones por concatenación.
_SEP = "\x1f"


def compute_checksum(
    up_sql: str,
    up_sql_mysql: str | None,
    up_sql_postgresql: str | None,
    down_sql: str | None = None,
    version: str | None = None,
) -> str:
    """
    SHA256 del SQL ejecutable (up + variantes + rollback) y de la ``version``.

    Incluir ``down_sql`` protege el rollback destructivo igual que el ``up_sql``.
    Incluir ``version`` impide que un tampering directo cambie el identificador que el
    runner usa para nombrar el archivo de revisión (anti path-traversal, junto con
    ``validate_version``).
    """
    parts = [
        up_sql or "",
        up_sql_mysql or "",
        up_sql_postgresql or "",
        down_sql or "",
        version or "",
    ]
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


def validate_version(version: str) -> str:
    """
    Valida ``version`` contra la whitelist o lanza 422. Se usa en el runner ANTES de
    construir nombres de archivo/identificadores a partir de ``version`` (los datos
    vienen de la BD del gateway; un tampering directo podría inyectar ``../``).
    """
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise AppHttpException(
            message="Versión de migración inválida (se esperaban solo dígitos).",
            status_code=422,
            context={"kind": "version"},
        )
    return version


def version_sort_key(version: str) -> int:
    """Clave de orden NUMÉRICA (no lexicográfica) para versiones de solo dígitos."""
    return int(version)
