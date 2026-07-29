"""
Token de confirmación firmado para operaciones destructivas a NIVEL SERVIDOR que no
tienen una fila persistida donde anclar un TTL (a diferencia de clone/schema-comparison,
que guardan ``confirm_token``/``expires_at`` en su ``CloneJob``/``SchemaComparison``).

Es un HMAC-SHA256 sobre ``(operación, server_id, nombre)`` con la expiración EMBEBIDA en
el propio token (``"{exp_epoch}.{hmac_hex}"``), firmado con ``SECRET_KEY``. Propiedades:

- **Stateless**: no requiere tabla ni limpieza; el ``preview`` lo emite y el ``execute`` lo
  re-verifica recomputando el HMAC server-side.
- **Ligado a la identidad física** ``(server_id, db_name)``: un token de una BD no sirve
  para otra ni para otro servidor.
- **Infalsificable** sin ``SECRET_KEY`` y con **TTL real** (default 2 minutos).

COMPLEMENTA —no reemplaza— la confirmación por nombre (``confirm_target_name == db_name``):
el nombre obliga a identificar CONSCIENTEMENTE cuál BD; el token da frescura/anti-replay.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from app.core.environments import SECRET_KEY
from app.exceptions import AppHttpException

_DEFAULT_TTL_SECONDS = 120


def _sign(operation: str, server_id: int, db_name: str, exp: int) -> str:
    msg = f"{operation}\x1f{server_id}\x1f{db_name}\x1f{exp}".encode("utf-8")
    key = (SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def issue(
    operation: str, server_id: int, db_name: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS
) -> tuple[str, datetime]:
    """Emite ``(token, expires_at)`` para ``(operation, server_id, db_name)``."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    exp = int(expires_at.timestamp())
    return f"{exp}.{_sign(operation, server_id, db_name, exp)}", expires_at


def verify(token: str, operation: str, server_id: int, db_name: str) -> None:
    """
    Valida el token. Lanza 422 si es inválido/no corresponde a esta BD, 410 si expiró.
    """
    if not token or "." not in token:
        raise AppHttpException(
            message="Token de confirmación ausente o inválido.",
            status_code=422,
            context={},
        )
    exp_str, mac = token.split(".", 1)
    try:
        exp = int(exp_str)
    except ValueError as exc:
        raise AppHttpException(
            message="Token de confirmación malformado.", status_code=422, context={}
        ) from exc
    if int(datetime.now(timezone.utc).timestamp()) > exp:
        raise AppHttpException(
            message="El token de confirmación expiró; vuelve a solicitar el preview.",
            status_code=410,
            context={},
        )
    expected = _sign(operation, server_id, db_name, exp)
    if not hmac.compare_digest(mac, expected):
        raise AppHttpException(
            message="El token de confirmación no corresponde a esta base de datos.",
            status_code=422,
            context={},
        )
