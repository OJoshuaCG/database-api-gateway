"""
Formato del bearer de integración: ``datumint.<public_id>.<secreto>``.

Módulo hoja (sin dependencias del gateway) por el mismo motivo que ``mcp_token_format``: lo
necesitan tanto el limitador (para su ``key_func``) como la autenticación, y definirlo en la
segunda obligaría al primero a importarla de vuelta y cerraría un ciclo.

El prefijo ``datumint`` es DISTINTO de los de agente (``datum``, ``dbgw``) a propósito: un
bearer de integración no puede parsear como de agente ni al revés, así que una credencial
presentada en el endpoint equivocado se rechaza en el parseo y nunca llega a la BD.

``parse_integration_bearer`` no levanta nunca y devuelve ``None`` ante cualquier entrada
inválida: el llamador responde con el MISMO 401 opaco para todo lo malformado, y una excepción
acá sería un 500 que distingue "malformado" de "inexistente".
"""

import re
from dataclasses import dataclass
from secrets import token_urlsafe

#: Prefijo con el que se EMITEN los tokens de integración.
INTEGRATION_TOKEN_PREFIX = "datumint"

#: Largo de la columna ``integration_tokens.token_id``. ``token_urlsafe(18)`` produce
#: exactamente 24 caracteres; el número tiene que coincidir con ``String(24)`` del modelo o el
#: INSERT falla en runtime.
INTEGRATION_PUBLIC_ID_LENGTH = 24
_PUBLIC_ID_RANDOM_BYTES = 18

#: Bytes aleatorios del secreto (256 bits). Se devuelve una sola vez y no se guarda: lo que
#: persiste es su HMAC.
_SECRET_RANDOM_BYTES = 32

#: Tope del largo del secreto aceptado al parsear. ``token_urlsafe(32)`` mide 43; el tope evita
#: que un bearer gigante se pague en HMAC antes de ser rechazado.
_MAX_SECRET_LENGTH = 128

#: Alfabeto de ``secrets.token_urlsafe``. Incluye ``_`` y ``-`` pero NO ``.``, que es lo que
#: permite usarlo como separador sin ambigüedad.
_URLSAFE_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")

_BEARER_PART_COUNT = 3


@dataclass(frozen=True, slots=True)
class IntegrationTokenParts:
    """Las dos partes de un bearer bien formado. El ``public_id`` indexa; el secreto se verifica."""

    public_id: str
    secret: str


def mint_integration_token() -> tuple[str, str, str]:
    """
    ``(public_id, secreto, bearer_completo)`` para un token de integración nuevo.

    El secreto se muestra UNA vez al crear y no se recupera: si se pierde, se emite otro.
    """
    public_id = token_urlsafe(_PUBLIC_ID_RANDOM_BYTES)
    secret = token_urlsafe(_SECRET_RANDOM_BYTES)
    return public_id, secret, f"{INTEGRATION_TOKEN_PREFIX}.{public_id}.{secret}"


def parse_integration_bearer(raw_token: object) -> IntegrationTokenParts | None:
    """
    Las partes de ``datumint.<public_id>.<secreto>``, o ``None`` si no es exactamente eso.

    ``raw_token`` es el valor DESPUÉS de ``Bearer ``. Acepta ``object`` y no solo ``str`` porque
    el llamador puede pasar un header ausente (``None``): este es el borde de entrada y no puede
    levantar.
    """
    if not isinstance(raw_token, str):
        return None
    parts = raw_token.split(".")
    if len(parts) != _BEARER_PART_COUNT:
        return None
    prefix, public_id, secret = parts
    if prefix != INTEGRATION_TOKEN_PREFIX:
        return None
    if not public_id or len(public_id) > INTEGRATION_PUBLIC_ID_LENGTH:
        return None
    if not secret or len(secret) > _MAX_SECRET_LENGTH:
        return None
    if _URLSAFE_PATTERN.fullmatch(public_id) is None:
        return None
    if _URLSAFE_PATTERN.fullmatch(secret) is None:
        return None
    return IntegrationTokenParts(public_id=public_id, secret=secret)
