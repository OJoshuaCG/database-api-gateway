"""
Excepciones a la SEPARACIÓN DE DEBERES: ``SodException``.

Una fila dice "esta cuenta PUEDE violar esta regla" (``app/core/separation_of_duties.py``). Sin
una fila viva que la cubra, el escritor responde 409 ``access.sod_conflict`` y el lector descarta
las capacidades de ``security_officer``.

DOS CLASES DE FILA
------------------
- **Heredada** (``reason='grandfathered'``, ``expires_at`` NULL): la combinación existía antes de
  la regla —el administrador sembrado tiene ``owner`` + ``access_admin`` + ``security_officer``—.
  La siembra la migración ``f8b0d2e4a6c9`` y ``bootstrap_admin``. No se parte sola porque eso
  puede dejar el gateway sin ``security_officer`` o sin ``owner``, y la persona no se lo puede
  arreglar (la auto-modificación está prohibida). Se reporta en cada arranque.
- **Break-glass** (``sod_override`` del payload): motivo obligatorio y vencimiento de 7 días
  como mucho. En C2 se aplica en el acto y se audita (``access.sod_override``); ``approved_by``
  queda NULL hasta que C3 lo enrute por el segundo aprobador.

VIVA = ``closed_at IS NULL AND (expires_at IS NULL OR expires_at > now)``. Las filas no se
borran: cuando la cuenta deja de violar la regla, se cierran (``closed_reason='resolved'``) para
que la excepción no quede como licencia permanente que cubra una combinación futura.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.services.capability_catalog import SOD_RULES

#: ``reason`` de una fila heredada. Junto con ``expires_at`` NULL la distingue de un override.
GRANDFATHERED_REASON = "grandfathered"
#: ``closed_reason`` cuando la cuenta dejó de violar la regla.
CLOSED_RESOLVED = "resolved"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class SodException(Base, TimestampMixin):
    __tablename__ = "sod_exceptions"
    __table_args__ = (
        Index("ix_sod_exceptions_user_rule", "user_id", "rule"),
        CheckConstraint(f"rule IN ({_in(SOD_RULES)})", name="rule"),
        {"comment": "Excepciones a la separación de deberes del gateway (plano de CONTROL)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la excepción"
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Usuario del gateway cubierto por la excepción",
    )
    rule: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Regla de separación de deberes que se exceptúa"
    )
    reason: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Motivo declarado ('grandfathered' si es heredada)"
    )
    requested_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Administrador que pidió el override (NULL si es heredada)",
    )
    approved_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Segundo aprobador (NULL hasta que exista el flujo de aprobación)",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Vencimiento (UTC); NULL = heredada, sin vencimiento"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cierre (UTC): la cuenta dejó de violar la regla"
    )
    closed_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="Motivo del cierre (resolved)"
    )

    def __repr__(self) -> str:
        return f"<SodException(user_id={self.user_id}, rule='{self.rule}')>"
