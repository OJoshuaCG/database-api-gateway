"""
Almacén de sesiones del gateway: crear, resolver, tachar.

Vive en ``core`` y no en ``controllers`` porque de acá cuelga la autenticación, y ``core`` no
puede importar controllers sin invertir la dependencia del proyecto.

LOS DOS VENCIMIENTOS, Y POR QUÉ SON DOS
---------------------------------------
- **Absoluto** contra ``created_at``: es lo único que un atacante con actividad continua no
  puede estirar. Antes no existía: con la sesión en la cookie firmada, la actividad la renovaba
  para siempre.
- **Inactividad** contra ``last_seen_at``: el default baja de 8 h a 60 min a propósito. Ocho
  horas es mucho para una herramienta que tiene credenciales pseudo-root sobre la producción de
  terceros y que se usa a ráfagas, no de corrido.

Los dos vencen **cerrando la sesión con motivo**, no devolviendo 401 a secas: si la fila no se
tacha, el mismo ``sid`` vuelve a intentarlo en cada request y la razón del corte no queda
registrada en ninguna parte.

EL UPDATE DE ``last_seen_at`` ESTÁ AMORTIGUADO
----------------------------------------------
Escribirlo en cada request sería un UPDATE por request sobre la BD de metadatos, y el gateway
sirve listados que la SPA refresca al reenfocar la ventana. Se escribe solo si pasó
``_LAST_SEEN_RESOLUTION``; el costo es que la inactividad se mide con esa granularidad, que
frente a un timeout de 60 minutos es irrelevante.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    SESSION_ABSOLUTE_MAX_HOURS,
    SESSION_IDLE_MINUTES,
)
from app.models.gateway_session import GatewaySession

#: Cada cuánto, como mucho, se escribe ``last_seen_at``. Ver el docstring del módulo.
_LAST_SEEN_RESOLUTION = timedelta(seconds=60)

#: Motivos de revocación. Vocabulario CERRADO: el `revoked_reason` se lee en un incidente y un
#: texto libre por sitio de llamada lo vuelve inagrupable.
REASON_LOGOUT = "logout"
REASON_PASSWORD_CHANGE = "password_change"
REASON_ROLE_CHANGE = "role_change"
REASON_ABSOLUTE = "absolute"
REASON_IDLE = "idle"
REASON_ADMIN_REVOKED = "admin_revoked"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _session():
    return Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT).get_declarative_base_session()


def new_sid() -> str:
    """
    128 bits de entropía en base64url (22 caracteres).

    ``token_urlsafe`` usa ``secrets``, o sea el CSPRNG del sistema. 128 bits es el mínimo
    razonable para un identificador que **es** la credencial de sesión: adivinarlo equivale a
    tener la cookie.
    """
    return token_urlsafe(16)


def ua_hash(user_agent: str | None) -> str | None:
    """SHA256 del User-Agent, o ``None``. Ver el comentario de la columna en el modelo."""
    if not user_agent:
        return None
    return sha256(user_agent.encode("utf-8", "replace")).hexdigest()


def create(user_id: int, *, ip: str | None, user_agent: str | None) -> str:
    """
    Abre una sesión y devuelve su ``sid``.

    Cada login crea una fila NUEVA en vez de reutilizar la del usuario, y eso es la **rotación
    del sid**: un `sid` fijado por un atacante antes del login (session fixation) deja de servir
    en el momento en que la víctima se autentica, porque la cookie pasa a llevar otro.
    """
    ahora = _utcnow()
    sid = new_sid()
    session = _session()
    try:
        session.add(
            GatewaySession(
                sid=sid,
                user_id=user_id,
                created_at=ahora,
                last_seen_at=ahora,
                ip=(ip or None),
                user_agent_hash=ua_hash(user_agent),
            )
        )
        session.commit()
    finally:
        session.close()
    return sid


def resolve(sid: str) -> tuple[int | None, str | None]:
    """
    ``(user_id, motivo_de_rechazo)``. Exactamente uno de los dos es ``None``.

    Devuelve el motivo en vez de solo ``None`` porque quien llama tiene que poder distinguir
    "sesión desconocida" de "vencida por absoluto" — el primero no merece rastro (es ruido de
    cookies viejas) y los dos segundos sí, porque explican por qué alguien fue expulsado.

    **Tacha la fila al vencer**, no solo rechaza: ver el docstring del módulo.
    """
    if not sid:
        return None, "missing"

    ahora = _utcnow()
    session = _session()
    try:
        fila = session.get(GatewaySession, sid)
        if fila is None:
            return None, "unknown"
        if fila.revoked_at is not None:
            return None, fila.revoked_reason or REASON_ADMIN_REVOKED

        if ahora - fila.created_at >= timedelta(hours=SESSION_ABSOLUTE_MAX_HOURS):
            fila.revoked_at, fila.revoked_reason = ahora, REASON_ABSOLUTE
            session.commit()
            return None, REASON_ABSOLUTE

        if ahora - fila.last_seen_at >= timedelta(minutes=SESSION_IDLE_MINUTES):
            fila.revoked_at, fila.revoked_reason = ahora, REASON_IDLE
            session.commit()
            return None, REASON_IDLE

        if ahora - fila.last_seen_at >= _LAST_SEEN_RESOLUTION:
            fila.last_seen_at = ahora
            session.commit()
        return fila.user_id, None
    finally:
        session.close()


def revoke(sid: str, reason: str) -> None:
    """
    Tacha UNA sesión. Idempotente: revocar una ya revocada no cambia el motivo original.

    Conservar el primer motivo importa: un ``logout`` posterior a una revocación por cambio de
    password reescribiría la razón real por la que la sesión terminó.
    """
    session = _session()
    try:
        fila = session.get(GatewaySession, sid)
        if fila is not None and fila.revoked_at is None:
            fila.revoked_at, fila.revoked_reason = _utcnow(), reason
            session.commit()
    finally:
        session.close()


def revoke_all_for_user(user_id: int, reason: str, *, except_sid: str | None = None) -> int:
    """
    Tacha TODAS las sesiones vivas de un usuario. Devuelve cuántas.

    ``except_sid`` existe para el caso que de otro modo es hostil: quien cambia su propia
    password espera cerrar las **otras** sesiones, no que lo echen de la que está usando. Para
    una revocación administrativa se omite y cae también la propia.
    """
    session = _session()
    try:
        q = session.query(GatewaySession).filter(
            GatewaySession.user_id == user_id,
            GatewaySession.revoked_at.is_(None),
        )
        if except_sid:
            q = q.filter(GatewaySession.sid != except_sid)
        ahora = _utcnow()
        n = 0
        for fila in q.all():
            fila.revoked_at, fila.revoked_reason = ahora, reason
            n += 1
        session.commit()
        return n
    finally:
        session.close()


def list_for_user(user_id: int, *, current_sid: str | None) -> list[dict]:
    """
    Las sesiones VIVAS del usuario, la actual primero.

    **Nunca devuelve el ``sid`` completo**, solo un prefijo de 8 caracteres. El `sid` ES la
    credencial de sesión: ponerlo en un cuerpo de respuesta lo mete en los logs del proxy, en
    el historial del navegador de quien copie la URL y al alcance de cualquier XSS. El prefijo
    alcanza para lo único que la pantalla necesita —distinguir una fila de otra— y no sirve para
    autenticarse.

    Tampoco devuelve el ``user_agent_hash``: es una huella y no dice nada legible. La IP sí,
    porque es lo que le permite al usuario reconocer "esto no fui yo".
    """
    session = _session()
    try:
        filas = (
            session.query(GatewaySession)
            .filter(
                GatewaySession.user_id == user_id,
                GatewaySession.revoked_at.is_(None),
            )
            .order_by(GatewaySession.last_seen_at.desc())
            .all()
        )
        return [
            {
                "sid_prefix": f.sid[:8],
                "current": f.sid == current_sid,
                "created_at": f.created_at,
                "last_seen_at": f.last_seen_at,
                "ip": f.ip,
            }
            for f in filas
        ]
    finally:
        session.close()
