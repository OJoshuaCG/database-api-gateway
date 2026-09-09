"""
Autenticación de agentes: ``Authorization: Bearer dbgw.<token_id>.<secreto>``.

Es la hermana de ``authenticated_user``, y las dos convergen en el mismo ``Actor`` a propósito:
**una sola comprobación de autorización y un solo vocabulario**, no dos políticas que divergen en
silencio.

EL KILL SWITCH VIVE ACÁ, Y ES UN CHOKE POINT ÚNICO
--------------------------------------------------
``MCP_ENABLED`` se evalúa en este módulo y no en cada tool: si estuviera repartido, apagarlo
dejaría de apagar el día que alguien agregue un camino nuevo y se olvide de la línea. Acá no hay
forma de entrar sin pasar.

EL FORMATO USA PUNTO Y NO GUION BAJO, Y NO ES ESTÉTICA
------------------------------------------------------
El alfabeto de ``secrets.token_urlsafe`` **incluye ``_``**, así que ``dbgw_<id>_<secreto>`` es
imparseable con ``split("_")`` y produce un 401 **intermitente e irreproducible** según qué
caracteres salieron en el secreto. Con punto, el split es exacto.

El prefijo ``dbgw.`` además hace el secreto matcheable por escáneres de secretos, que es lo que
puede detectarlo cuando termine commiteado en el repo de otra gente.

POR QUÉ HMAC Y NO ARGON2
------------------------
Argon2 saltea por hash, o sea **no es indexable**: verificar sería O(N) verificaciones Argon2 por
request, lento y un vector de DoS de CPU con bearers basura. Argon2 estira entropía baja; un
secreto de 256 bits no la tiene. Un ``token_id`` inexistente **igual paga un HMAC** contra una
constante, para no filtrar existencia por tiempo — el mismo criterio que el ``_DUMMY_HASH`` del
login.
"""

from datetime import UTC, datetime, timedelta
from hmac import compare_digest, new as hmac_new
from hashlib import sha256

from fastapi import Request

from app.core.actor import Actor, token_actor
from app.core.crypto import api_token_pepper
from app.core.environments import MCP_ENABLED
from app.exceptions import AppHttpException
from app.models.api_token import ApiToken

#: Prefijo del bearer. Ver el docstring del módulo.
TOKEN_PREFIX = "dbgw"

#: Cada cuánto, como mucho, se escribe ``last_used_at``. Un UPDATE por request es amplificación
#: de escritura gratis sobre la BD de metadatos.
_LAST_USED_RESOLUTION = timedelta(seconds=60)

#: Un ÚNICO código opaco para todo fallo de credencial: inexistente, expirado, revocado y
#: malformado responden igual. Distinguirlos convertiría el endpoint en un oráculo del estado de
#: los tokens que alguien haya adivinado.
CODE_TOKEN_INVALID = "mcp.token_invalid"
CODE_DISABLED = "mcp.disabled"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _session():
    from app.core.database import Database

    return Database().get_declarative_base_session()


def token_hmac(secret: str) -> str:
    """HMAC-SHA256 del secreto con el pepper, en hex."""
    return hmac_new(api_token_pepper(), secret.encode("utf-8"), sha256).hexdigest()


def mint() -> tuple[str, str, str]:
    """
    ``(token_id, secreto, bearer_completo)`` para un token nuevo.

    El secreto se devuelve UNA vez y no se guarda: lo que persiste es su HMAC. Si alguien lo
    pierde, se emite otro — no hay recuperación, y eso es lo correcto.
    """
    from secrets import token_urlsafe

    # 18 bytes → 24 caracteres URL-safe, que es exactamente el largo de la columna. El número
    # tiene que coincidir con `String(24)` del modelo o el INSERT falla en runtime.
    token_id = token_urlsafe(18)
    secret = token_urlsafe(32)
    return token_id, secret, f"{TOKEN_PREFIX}.{token_id}.{secret}"


def _reject() -> AppHttpException:
    return AppHttpException(
        message="Credencial de agente inválida.",
        status_code=401,
        public_context={"code": CODE_TOKEN_INVALID},
    )


def authenticate_agent(request: Request) -> Actor:
    """
    Resuelve el bearer a un ``Actor`` de tipo ``api_token``. Levanta 503 o 401.

    **No mira cookies, y eso es un invariante**: un token nunca autentica por cookie y una
    cookie nunca autentica por ``Authorization``. Si los dos caminos se pudieran mezclar, la
    exención de CSRF que tienen los agentes —correcta, porque un bearer no es ambiental— se
    volvería el bypass.
    """
    if not MCP_ENABLED:
        # 503 y no 404: el operador que lo prendió y no anda necesita saber que está apagado,
        # no buscar un endpoint que "no existe". Y no revela nada: el kill switch es config, no
        # un secreto.
        raise AppHttpException(
            message="El servidor MCP está deshabilitado.",
            status_code=503,
            public_context={"code": CODE_DISABLED},
        )

    crudo = request.headers.get("authorization") or ""
    if not crudo.lower().startswith("bearer "):
        raise _reject()
    partes = crudo[7:].strip().split(".")
    if len(partes) != 3 or partes[0] != TOKEN_PREFIX:
        raise _reject()
    _, token_id, secreto = partes

    ahora = _utcnow()
    session = _session()
    try:
        fila = (
            session.query(ApiToken).filter(ApiToken.token_id == token_id).one_or_none()
        )
        if fila is None:
            # Se paga el HMAC igual: sin esto, un `token_id` inexistente responde más rápido y
            # eso enumera qué ids existen.
            token_hmac(secreto)
            raise _reject()

        if not compare_digest(fila.secret_hmac, token_hmac(secreto)):
            raise _reject()
        if fila.revoked_at is not None or fila.expires_at <= ahora:
            raise _reject()

        if fila.last_used_at is None or ahora - fila.last_used_at >= _LAST_USED_RESOLUTION:
            fila.last_used_at = ahora
            session.commit()

        return token_actor(
            token_pk=fila.id,
            token_id=fila.token_id,
            name=fila.name,
            scopes=fila.scopes,
            project_id=fila.project_id,
        )
    finally:
        session.close()
