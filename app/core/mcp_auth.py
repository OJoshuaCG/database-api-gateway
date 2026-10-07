"""
Autenticación de agentes: ``Authorization: Bearer datum.<token_id>.<secreto>``.

Los tokens emitidos antes del cambio de nombre del producto usan ``dbgw.<token_id>.<secreto>`` y
**se siguen aceptando**. El prefijo NO entra en el HMAC (se calcula solo sobre el secreto) ni en
la búsqueda (se indexa por ``token_id``), así que un token legado verifica con su prefijo
original sin migrar ninguna fila. Solo la EMISIÓN usa el prefijo nuevo.

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
El alfabeto de ``secrets.token_urlsafe`` **incluye ``_``**, así que ``datum_<id>_<secreto>`` es
imparseable con ``split("_")`` (aplica igual a ``datum_`` que a ``dbgw_``) y produce un 401 **intermitente e irreproducible** según qué
caracteres salieron en el secreto. Con punto, el split es exacto.

El prefijo (``datum.``, y ``dbgw.`` en los legados) además hace el secreto matcheable por
escáneres de secretos, que es lo que puede detectarlo cuando termine commiteado en el repo de
otra gente. Por eso las reglas de escaneo tienen que cubrir **los dos** prefijos.

TODO INTENTO DEJA RASTRO, Y EL MOTIVO NO VIAJA EN LA RESPUESTA
--------------------------------------------------------------
La versión anterior de este módulo **no auditaba nada**: ni los fallos de credencial ni las
sesiones válidas. La asimetría era la que importaba — ``auth.login`` de un humano sí se
registraba, y la credencial con más probabilidad de terminar commiteada en el repo de otra gente
era la única cuyo uso no dejaba huella. No se podía responder "¿alguien está probando tokens?"
ni "¿desde cuándo se usa este token que acabo de revocar?".

Ahora todo camino deja rastro. Y la distinción que hay que preservar: **el ``detail`` lleva
el motivo interno** —inexistente, hmac, revocado, expirado— mientras la **respuesta** sigue
siendo el único código opaco. El oráculo se le niega al atacante, no al operador.

LOS RECHAZOS SE LIMITAN POR IP Y SE AUDITAN AGREGADOS
-----------------------------------------------------
"Una fila por rechazo" era una escritura gratis en ``audit_log`` para cualquiera sin
credencial: el límite por token del ``mcp_limiter`` no frena a quien inventa un ``token_id``
distinto por request (cada uno estrena un cupo), así que cada request basura pagaba una lectura
a la BD de metadatos, un HMAC y un INSERT — el DoS sobre la BD compartida que la sub-app decía
haber cerrado, más el registro real enterrado bajo ruido. Dos piezas lo cierran:

- **Tope de rechazos por IP** (``MCP_AUTH_FAILURE_RATE_LIMIT``): cada rechazo consume un cupo de
  la IP, y con el cupo agotado la IP recibe 429 **antes** de tocar la BD. El costo, declarado: un
  agente legítimo detrás de la misma IP que el atacante (un runner de CI compartido) también
  recibe 429 mientras dure la ventana. Se acepta porque es acotado y se disuelve solo; la
  alternativa es que cualquiera sature la BD de metadatos.
- **Auditoría agregada** (``_rejection_audit``, un ``WindowedAggregator``): como mucho una fila por IP por ventana, y esa
  fila lleva cuántos rechazos de la IP quedaron sin fila desde la anterior.

EL TOKEN HEREDA, Y NUNCA SUPERA, LOS PERMISOS DE QUIEN LO EMITIÓ
-----------------------------------------------------------------
Un token no es una identidad independiente: es una delegación de ``created_by_admin_id``. Tras
validar el bearer se carga al emisor y sus capacidades EFECTIVAS (capa 1: rol unión, globales y
puntuales) acotan las del token: ``scopes ∩ techo de agente ∩ capacidades del emisor`` (ver
``token_actor``). Se relee en cada request, así que degradar al emisor degrada sus tokens.

Se rechaza (motivo de auditoría ``emisor_inactivo``, respuesta opaca ``mcp.token_invalid``) si el
emisor es NULL, no existe o está desactivado. **Los tokens legados con ``created_by_admin_id``
NULL quedan rechazados**: hay que reemitirlos.

Límite declarado: esto acota CAPACIDADES, no el alcance por entorno. El modelo de roles no tiene
denegación por entorno (un ``viewer`` lee todo), así que el alcance de destino de un token sigue
siendo solo su ``project_id``.

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
from time import monotonic

from fastapi import Request
from limits import parse
from slowapi.util import get_remote_address

from app.core.actor import Actor, token_actor
from app.core.authz import actor_from_access_context
from app.core.audit_aggregator import WindowedAggregator
from app.core.crypto import api_token_pepper
from app.core.environments import MCP_AUTH_FAILURE_RATE_LIMIT, MCP_ENABLED
from app.core.limiter import hit_or_429, mcp_limiter
from app.core.mcp_token_format import ACCEPTED_TOKEN_PREFIXES, TOKEN_PREFIX
from app.exceptions import AppHttpException
from app.models.api_token import ApiToken
from app.models.user_model import UserModel

# ``TOKEN_PREFIX`` (emisión), ``LEGACY_TOKEN_PREFIX`` y ``ACCEPTED_TOKEN_PREFIXES`` (parseo)
# viven en ``mcp_token_format`` para que ``limiter`` los comparta sin ciclo. Ver el docstring del
# módulo.

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


#: Ventana de agregación de la auditoría de rechazos: como mucho una fila por IP por ventana.
_AUDIT_WINDOW_SECONDS = 60.0
#: Tope de IPs que el agregador recuerda. Sin tope, un atacante con un bloque IPv6 haría crecer
#: el diccionario sin límite: el agregador que existe para acotar un recurso sería otro sin cota.
_AUDIT_MAX_IPS = 10_000


#: Agregador de rechazos, por IP. La semántica de la cuenta y sus límites están en
#: ``app.core.audit_aggregator``. El reloj se lee de ESTE módulo (``monotonic``) para que los
#: tests puedan avanzarlo parcheando ``mcp_auth.monotonic``.
_rejection_audit = WindowedAggregator(
    window=_AUDIT_WINDOW_SECONDS, max_keys=_AUDIT_MAX_IPS, clock=lambda: monotonic()
)


def reset_rejection_state() -> None:
    """
    Olvida el agregador y los cupos del ``mcp_limiter``. Para los tests: el storage en memoria
    vive lo que el proceso, y sin esto los rechazos de un test le gastan el cupo al siguiente.
    """
    _rejection_audit.reset()
    mcp_limiter.reset()


def _client_ip(request: Request) -> str:
    return get_remote_address(request) or "desconocida"


def _failure_quota_exhausted(ip: str) -> bool:
    """
    ¿La IP ya gastó su cupo de rechazos? ``test`` y no ``hit``: consultar no consume. El
    consumo lo hace ``_reject``, uno por rechazo real.
    """
    if not mcp_limiter.enabled:
        return False
    return not mcp_limiter.limiter.test(
        parse(MCP_AUTH_FAILURE_RATE_LIMIT), "mcp_auth_failure", ip
    )


def _audit_rejection(ip: str, motivo: str, token_id: str | None = None) -> None:
    """La fila de rechazo, solo si el agregador la admite. Best-effort, como toda auditoría."""
    agregados = _rejection_audit.admit(ip)
    if agregados is None:
        return
    from app.services import audit

    audit.record(
        "mcp.auth",
        status="failure",
        admin=None,
        # Quien falla la autenticación NO es el token que dice ser: sin esto la fila caía en el
        # default ``"admin"`` de ``actor_type_of(None)`` y un rechazo leía como una acción de un
        # administrador anónimo. El ``token_id`` reclamado va en el detalle, no como autor.
        actor_type="anonymous",
        target_type="api_token",
        touched_engine=False,
        # `token_id` es la parte PÚBLICA del bearer. El secreto no aparece, ni entero ni
        # recortado: un prefijo de secreto en un log es un secreto en un log.
        detail=(
            f"rechazo={motivo}"
            + (f" token={token_id}" if token_id else "")
            + f" ip={ip} agregados={agregados}"
        ),
    )


def _reject(
    request: Request, motivo: str, *, token_id: str | None = None
) -> AppHttpException:
    """
    El 401 opaco, con el motivo en la AUDITORÍA y no en la respuesta.

    ``motivo`` es vocabulario cerrado (``sin_bearer``, ``malformado``, ``inexistente``,
    ``hmac``, ``revocado``, ``expirado``, ``emisor_inactivo``) porque se lee en un incidente y un texto libre por
    sitio de llamada lo vuelve inagrupable.

    Consume un cupo de rechazos de la IP; si ese era el último, el que levanta es el 429 y no
    el 401 (los dos revelan lo mismo: que la credencial no sirvió).

    Best-effort: un fallo al auditar no puede convertirse en un fallo al rechazar. Lo
    fail-closed está reservado a lo que divulga datos, y acá no se divulga nada.
    """
    ip = _client_ip(request)
    _audit_rejection(ip, motivo, token_id)
    hit_or_429(mcp_limiter, MCP_AUTH_FAILURE_RATE_LIMIT, "mcp_auth_failure", ip)
    return AppHttpException(
        message="Credencial de agente inválida.",
        status_code=401,
        public_context={"code": CODE_TOKEN_INVALID},
    )


def _load_issuer(created_by_admin_id: int | None) -> Actor | None:
    """
    El ``Actor`` del usuario que emitió el token, o ``None`` si no sirve como emisor.

    ``None`` cubre tres casos y los tres rechazan: ``created_by_admin_id`` NULL (token legado o
    sin autor), usuario inexistente (la columna no tiene FK a propósito) y usuario desactivado.
    La existencia se verifica ANTES de leer el contexto porque ``find_access_context`` de un id
    inexistente cae en silencio al rol ``viewer``, y eso resolvería un emisor fantasma con
    permisos de lectura.
    """
    if created_by_admin_id is None:
        return None
    modelo = UserModel()
    usuario = modelo.find_by_id(created_by_admin_id)
    if not usuario or not usuario.get("is_active"):
        return None
    return actor_from_access_context(
        usuario["id"], usuario["username"], modelo.find_access_context(usuario["id"])
    )


def authenticate_agent(request: Request) -> Actor:
    """
    Resuelve el bearer a un ``Actor`` de tipo ``api_token``. Levanta 503 o 401.

    **No mira cookies, y eso es un invariante**: un token nunca autentica por cookie y una
    cookie nunca autentica por ``Authorization``. Si los dos caminos se pudieran mezclar, la
    exención de CSRF que tienen los agentes —correcta, porque un bearer no es ambiental— se
    volvería el bypass.
    """
    ip = _client_ip(request)
    if not MCP_ENABLED:
        # También se audita el rechazo por kill switch: si alguien lo apagó y los agentes siguen
        # intentando, eso es información operativa que hay que poder ver. Agregado como los
        # demás: con el servidor apagado, una fila por request seguía siendo escritura gratis.
        _audit_rejection(ip, "deshabilitado")
        # 503 y no 404: el operador que lo prendió y no anda necesita saber que está apagado,
        # no buscar un endpoint que "no existe". Y no revela nada: el kill switch es config, no
        # un secreto.
        raise AppHttpException(
            message="El servidor MCP está deshabilitado.",
            status_code=503,
            public_context={"code": CODE_DISABLED},
        )

    if _failure_quota_exhausted(ip):
        # Antes de leer la BD y antes del HMAC: es lo que hace que el tope sea un tope de COSTO y
        # no solo de filas. Se cuenta en el agregador para que la fila siguiente diga el volumen.
        _audit_rejection(ip, "limite_ip")
        hit_or_429(mcp_limiter, MCP_AUTH_FAILURE_RATE_LIMIT, "mcp_auth_failure", ip)

    crudo = request.headers.get("authorization") or ""
    if not crudo.lower().startswith("bearer "):
        raise _reject(request, "sin_bearer")
    partes = crudo[7:].strip().split(".")
    if len(partes) != 3 or partes[0] not in ACCEPTED_TOKEN_PREFIXES:
        raise _reject(request, "malformado")
    _, token_id, secreto = partes

    ahora = _utcnow()
    session = _session()
    try:
        fila = (
            session.query(ApiToken).filter(ApiToken.token_id == token_id).one_or_none()
        )
        if fila is None:
            # Se paga el HMAC igual: achica la señal de tiempo. El argumento de seguridad, sin
            # embargo, es la entropía del identificador — ver el docstring del módulo.
            token_hmac(secreto)
            raise _reject(request, "inexistente", token_id=token_id)

        if not compare_digest(fila.secret_hmac, token_hmac(secreto)):
            raise _reject(request, "hmac", token_id=token_id)
        if fila.revoked_at is not None:
            raise _reject(request, "revocado", token_id=token_id)
        if fila.expires_at <= ahora:
            raise _reject(request, "expirado", token_id=token_id)

        # El token delega a su emisor: se relee en CADA request (igual que ``is_active`` en la
        # sesión humana), así que desactivar o degradar al emisor surte efecto de inmediato.
        emisor = _load_issuer(fila.created_by_admin_id)
        if emisor is None:
            raise _reject(request, "emisor_inactivo", token_id=token_id)

        if fila.last_used_at is None or ahora - fila.last_used_at >= _LAST_USED_RESOLUTION:
            fila.last_used_at = ahora
            session.commit()

        from app.services import audit

        # El USO también se audita, no solo el fallo. Sin esto, un token robado podía enumerar
        # toda la superficie de tools (`initialize`, `tools/list`, `ping`) sin aparecer nunca en
        # el registro: el `_audit` del dispatch solo cubre `tools/call`.
        actor = token_actor(
            token_pk=fila.id,
            token_id=fila.token_id,
            name=fila.name,
            scopes=fila.scopes,
            project_id=fila.project_id,
            issuer=emisor,
        )
        # Con el actor YA resuelto: la fila queda ``actor_type='api_token'`` y
        # ``api_token_id=<pk>``. Con ``admin=None`` (la versión anterior) el filtro forense
        # ``actor_type='api_token'`` no veía ninguna autenticación de agente.
        audit.record(
            "mcp.auth",
            admin=actor,
            target_type="api_token",
            target_id=fila.id,
            touched_engine=False,
            detail=f"token={fila.token_id} proyecto={fila.project_id}",
        )
        return actor
    finally:
        session.close()
