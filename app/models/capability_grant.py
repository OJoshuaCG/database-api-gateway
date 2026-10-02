"""
Modelo de las CAPACIDADES PUNTUALES del gateway: ``CapabilityGrant``.

OJO CON EL PLANO. Esto es el plano de CONTROL —qué puede hacer un usuario **del gateway**—, no
los privilegios del motor. Ver el docstring de ``app/services/capability_catalog.py``.

POR QUÉ UNA TABLA APARTE DE ``access_grants``
---------------------------------------------
``access_grants`` guarda un ROL de la cadena (``viewer ⊆ operator ⊆ owner``) con su alcance, y
el resolvedor toma el máximo. Una capacidad suelta no es comparable con esa cadena: se SUMA al
rol efectivo del alcance, tiene ciclo de vida propio (pendiente de un segundo aprobador,
vencida, revocada) y deja historia. Mezclarla en ``access_grants`` rompería la monotonía que
hace bien definido el ``max``. ``PUT /access`` nunca toca esta tabla.

POR QUÉ ``live_key`` (D1)
-------------------------
Hay que impedir DOS filas vivas (pendiente o activa) para el mismo usuario, capacidad y alcance,
pero permitir cualquier cantidad de filas terminales (historial). Un índice único parcial no es
portable (MySQL 8 no lo tiene; el truco funcional no existe en MariaDB 11, ver
``app/models/environment.py``). La salida portable usa la misma propiedad que
``access_grant.py`` advierte, esta vez A PROPÓSITO: en un ``UNIQUE`` los ``NULL`` son distintos
en MySQL, MariaDB, PostgreSQL y SQLite. ``live_key`` vale ``1`` mientras la fila está viva y
``NULL`` cuando es terminal; un ``CHECK`` lo ata a ``status`` para que no puedan divergir. Un
insert concurrente pierde con ``IntegrityError``.

POR QUÉ NO HAY SENTINELA NI FK EN ``scope_id`` (D2)
---------------------------------------------------
El alcance global está prohibido, así que el sentinela ``0`` de ``access_grants`` no tiene
función: un ``CHECK`` exige ``scope_id > 0``. Sin FK porque el destino es polimórfico
(entorno o servidor); la existencia se valida en el controller.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

#: Estados vivos: los únicos con ``live_key = 1``. Solo ``active`` concede algo.
LIVE_STATUSES: tuple[str, ...] = ("pending", "active")
#: Estados terminales: historial, ``live_key`` NULL.
TERMINAL_STATUSES: tuple[str, ...] = ("rejected", "expired", "cancelled", "revoked")
ALL_STATUSES: tuple[str, ...] = LIVE_STATUSES + TERMINAL_STATUSES

SCOPE_TYPES: tuple[str, ...] = ("environment", "server")


def _utcnow() -> datetime:
    """UTC naive, la convención de las columnas ``DateTime`` del repo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class CapabilityGrant(Base, TimestampMixin):
    """
    Una capacidad individual sobre un entorno o servidor. Un usuario puede tener varias.

    Las filas no se borran: revocar, rechazar, vencer o cancelar cambian ``status`` y apagan
    ``live_key``. Solo las ``active`` conceden; el lector además descarta toda fila cuya
    capacidad sea desconocida o no otorgable (fail-closed).
    """

    __tablename__ = "capability_grants"
    __table_args__ = (
        # Dos filas vivas iguales son imposibles: `live_key` es 1 o NULL, y los NULL no chocan.
        UniqueConstraint(
            "user_id",
            "capability",
            "scope_type",
            "scope_id",
            "live_key",
            name="uq_capability_grants_live",
        ),
        Index("ix_capability_grants_user_status", "user_id", "status"),
        Index("ix_capability_grants_status_expires", "status", "expires_at"),
        CheckConstraint(
            f"scope_type IN ({_in(SCOPE_TYPES)}) AND scope_id > 0",
            name="scope",
        ),
        CheckConstraint(f"status IN ({_in(ALL_STATUSES)})", name="status"),
        # `live_key IS NOT NULL` explícito: con NULL, `live_key = 1` da NULL y un CHECK que
        # evalúa a NULL PASA, así que una fila viva sin `live_key` se saltearía el UNIQUE.
        CheckConstraint(
            f"(status IN ({_in(LIVE_STATUSES)}) AND live_key IS NOT NULL AND live_key = 1) "
            f"OR (status NOT IN ({_in(LIVE_STATUSES)}) AND live_key IS NULL)",
            name="live_key_status",
        ),
        {"comment": "Capacidades puntuales del gateway por usuario y alcance (plano de CONTROL)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la capacidad puntual"
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        # CASCADE: una capacidad sin usuario es basura; la historia vive en `audit_log`.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=False,
        comment="Usuario del gateway que recibe la capacidad",
    )

    capability: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Capacidad del catálogo (modulo.accion), otorgable"
    )

    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="Eje del alcance: environment | server"
    )

    scope_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Id del entorno o servidor (> 0, sin FK: destino polimórfico)"
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="pending",
        comment="pending | active | rejected | expired | cancelled | revoked",
    )

    live_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
        comment="1 si la fila está viva (pending/active), NULL si es terminal; base del UNIQUE",
    )

    requested_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Administrador que pidió u otorgó la capacidad",
    )

    decided_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Administrador que aprobó, rechazó o revocó",
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        comment="Fecha y hora (UTC) de la solicitud u otorgamiento",
    )

    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Fecha y hora (UTC) de la decisión"
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Vencimiento de una solicitud pendiente (7 días); NULL en las demás",
    )

    request_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Motivo declarado al pedir la capacidad"
    )

    decision_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Motivo de la decisión (rechazo o revocación)"
    )

    sod_override_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="sod_override pedido junto con la solicitud: se aplica al APROBARLA (C3)",
    )

    def __repr__(self) -> str:
        return (
            f"<CapabilityGrant(user_id={self.user_id}, capability='{self.capability}', "
            f"scope={self.scope_type}:{self.scope_id}, status='{self.status}')>"
        )
