"""
Modelo GatewaySession — una sesión de administrador, del lado del SERVIDOR.

POR QUÉ ESTA TABLA ES UN PREREQUISITO Y NO UNA MEJORA
-----------------------------------------------------
Con la sesión viviendo entera en la cookie firmada, el ``SessionMiddleware`` de Starlette
**re-firma con timestamp nuevo en CADA respuesta** y ``unsign(max_age=…)`` valida contra ese
timestamp fresco. Consecuencia medida, no teórica: ``SESSION_MAX_AGE`` es timeout de
**inactividad puro**, así que con actividad continua **la sesión no expira nunca**, y
``session.clear()`` borra la cookie *del cliente* — quien tenga una copia sigue autenticado.

O sea que cuatro controles eran inimplementables: vida absoluta, logout de verdad, revocación al
cambiar de rol o de password, y tope de sesiones concurrentes. Ninguno se arregla ajustando
parámetros: hacen falta un identificador opaco y una fila que el servidor pueda tachar.

**La cookie pasa a llevar SOLO el ``sid``.** Sigue firmada (eso protege contra manipulación),
pero deja de ser la fuente de verdad.

EL ANCLA DEL ABSOLUTO ES ``created_at``, NO ``last_seen_at``
-----------------------------------------------------------
Los dos timestamps existen porque miden cosas distintas y las dos hacen falta: ``created_at``
es el ancla de la vida **absoluta** —lo único que un atacante con actividad continua no puede
estirar— y ``last_seen_at`` la de la **inactividad**. Con uno solo, el control que falta es
precisamente el que importa contra una sesión robada que se usa.

POR QUÉ ``revoked_at`` Y NO UN DELETE
-------------------------------------
Borrar la fila deja la revocación indistinguible de una sesión que nunca existió, y con ella se
va la correlación forense con ``audit_log``: "esta operación vino de la sesión que se revocó por
cambio de password" es exactamente la pregunta que se hace después de un incidente.
``revoked_reason`` guarda el motivo con vocabulario cerrado, no texto libre.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class GatewaySession(Base):
    __tablename__ = "gateway_sessions"
    __table_args__ = ({"comment": "Sesiones de administrador, con vida absoluta y revocación"},)

    # Sin `TimestampMixin` a propósito: su `updated_at` se toca en cada UPDATE y acá el UPDATE
    # frecuente es el de `last_seen_at`, así que serían dos columnas diciendo lo mismo. Y
    # `created_at` acá no es metadato de auditoría de la fila: es el ancla del control absoluto,
    # así que se declara explícito para que nadie lo "normalice" a un mixin y le cambie la
    # semántica sin darse cuenta.
    sid: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="Identificador opaco de 128 bits en base64url. Es lo ÚNICO que viaja en la cookie",
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        # RESTRICT y no CASCADE: borrar un usuario no puede borrar el rastro de sus sesiones,
        # que es justo lo que se necesita después. El ciclo de vida de la cuenta es
        # desactivar, nunca borrar (§8.2 del plan).
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Dueño de la sesión",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="Inicio de la sesión (UTC). ANCLA de la vida absoluta: no se actualiza nunca",
    )

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="Último request visto (UTC). Ancla del timeout de INACTIVIDAD",
    )

    ip: Mapped[str | None] = mapped_column(
        String(45), nullable=True, comment="IP del login (IPv6 completo: 45 caracteres)"
    )

    # HASH y no el user-agent en claro: la cadena completa es una huella del navegador y del
    # equipo, o sea dato personal que no hace falta guardar. El hash alcanza para lo único que
    # se usa: detectar que la sesión cambió de cliente.
    user_agent_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="SHA256 del User-Agent del login"
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
        comment="Cuándo se tachó la sesión (UTC). NULL = viva",
    )

    revoked_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="logout | password_change | role_change | absolute | idle | admin_revoked",
    )

    def __repr__(self) -> str:
        return f"<GatewaySession(sid='{self.sid[:8]}…', user_id={self.user_id})>"
