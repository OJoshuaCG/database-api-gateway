"""
Solicitudes de ELEVACIÓN de acceso con segundo aprobador: ``AccessChangeRequest`` (C3).

Una fila es "esta cuenta debería quedar con ESTE acceso" (``desired_state_json``: rol base,
globales, alcances y, si hace falta, el ``sod_override``), pedida por un ``access_admin`` y
pendiente de que OTRO la apruebe. Nace cuando ``POST /gateway-users``, ``PATCH`` (rol) o
``PUT /access`` traen una elevación (``needs_second_approver``); la parte que no eleva ya se
aplicó en ese mismo request. Ver ``app/core/assignment_policy.py``.

``before_hash`` es la huella del acceso de la persona al pedirla (después de aplicar la parte
inmediata). Aprobar recalcula la huella: si difiere, el acceso cambió entretanto y la solicitud
se cancela con ``access.request_stale`` en vez de pisar un cambio que nadie revisó junto con ella.

Estados: ``pending`` → ``applied`` | ``rejected`` | ``cancelled`` | ``expired``. Las filas no se
borran; ``reason`` guarda el motivo de la decisión (o el automático: ``expired``,
``requester_lost_access``, ``superseded``, ``stale``).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

STATUS_PENDING = "pending"
STATUSES: tuple[str, ...] = ("pending", "applied", "rejected", "cancelled", "expired")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class AccessChangeRequest(Base, TimestampMixin):
    __tablename__ = "access_change_requests"
    __table_args__ = (
        Index("ix_access_change_requests_status_expires", "status", "expires_at"),
        Index("ix_access_change_requests_target_status", "target_user_id", "status"),
        CheckConstraint(f"status IN ({_in(STATUSES)})", name="status"),
        {"comment": "Elevaciones de acceso del gateway pendientes de un segundo aprobador"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la solicitud"
    )
    target_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Usuario del gateway cuyo acceso se eleva",
    )
    requested_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="access_admin que pidió la elevación",
    )
    desired_state_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Acceso final pedido (rol base, globales, alcances, sod_override, elevaciones)",
    )
    before_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="SHA-256 del acceso al pedirla (detecta solicitudes viejas)"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=STATUS_PENDING,
        comment="pending | applied | rejected | cancelled | expired",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="Vencimiento de la solicitud pendiente (UTC, 7 días)"
    )
    decided_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="access_admin que aprobó, rechazó o canceló (NULL si fue automático)",
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Fecha y hora (UTC) de la decisión"
    )
    reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Motivo de la decisión o del cierre automático"
    )

    def __repr__(self) -> str:
        return (
            f"<AccessChangeRequest(id={self.id}, target={self.target_user_id}, "
            f"status='{self.status}')>"
        )
