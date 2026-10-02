"""
Ventana de ARRANQUE de los accesos: ``AccessBootstrap`` (C4). Una sola fila (``id = 1``).

POR QUÉ EXISTE
--------------
Desde C3 toda elevación (``owner``, cualquier global, una capacidad exclusiva de ``owner``) espera
a un SEGUNDO ``access_admin``. Una instalación nueva tiene uno solo, y crear el segundo es en sí
una elevación: sin una salida, el primer arranque no podría completar nada. La ventana es esa
salida, acotada: mientras está abierta y hay un solo ``access_admin`` activo con credencial, sus
elevaciones se aplican en el acto y cada una se audita ``access.bootstrap_assignment``.

ESTADOS DE LA FILA
------------------
- **Por abrir** (``opened_at`` y ``closed_at`` NULL): la dejó la migración en una instalación con
  ≤ 1 ``access_admin``. El primer arranque la abre (``opened_at`` = ese arranque).
- **Abierta** (``opened_at`` fijado, ``closed_at`` NULL): vence en ``closes_at``.
- **Cerrada** (``closed_at`` fijado): para siempre. Solo ``ADMIN_RECOVERY=1`` la reabre.

``closed_reason``: ``second_admin`` (un segundo ``access_admin`` activo aceptó su invitación),
``deadline`` (venció ``ACCESS_BOOTSTRAP_WINDOW_HOURS``) o ``multiple_admins_at_upgrade`` (la
instalación ya tenía dos o más al migrar: nunca necesitó la ventana).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

#: La única fila. El ``CHECK`` impide una segunda: dos ventanas serían dos verdades.
SINGLETON_ID = 1

CLOSED_SECOND_ADMIN = "second_admin"
CLOSED_DEADLINE = "deadline"
CLOSED_MULTIPLE_ADMINS = "multiple_admins_at_upgrade"
CLOSED_REASONS: tuple[str, ...] = (CLOSED_SECOND_ADMIN, CLOSED_DEADLINE, CLOSED_MULTIPLE_ADMINS)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class AccessBootstrap(Base, TimestampMixin):
    __tablename__ = "access_bootstrap"
    __table_args__ = (
        CheckConstraint(f"id = {SINGLETON_ID}", name="singleton"),
        CheckConstraint(
            f"closed_reason IS NULL OR closed_reason IN ({_in(CLOSED_REASONS)})",
            name="closed_reason",
        ),
        {"comment": "Ventana de arranque de los accesos del gateway (fila única)"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, comment="Siempre 1: la fila es única"
    )
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Apertura (UTC); NULL = la abre el primer arranque"
    )
    closes_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Vencimiento de la ventana (UTC)"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cierre definitivo (UTC); NULL = abierta o por abrir"
    )
    closed_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="second_admin | deadline | multiple_admins_at_upgrade",
    )

    def __repr__(self) -> str:
        return f"<AccessBootstrap(opened_at={self.opened_at}, closed_at={self.closed_at})>"
