"""
Modelo SQL de la ventana de arranque (``access_bootstrap``, fila única).

Toda transición es un UPDATE CONDICIONAL sobre el estado de partida (``closed_at IS NULL``, etc.)
y devuelve si ESTA llamada ganó: con varios pods arrancando a la vez, o dos requests decidiendo
una elevación en el mismo instante, la ventana se abre una vez y se cierra una vez, y solo quien
ganó audita. El alta usa la PK fija (``id = 1``): dos altas simultáneas chocan en la PK y la
perdedora relee.
"""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.models.access_bootstrap import SINGLETON_ID, AccessBootstrap


def utcnow() -> datetime:
    """UTC naive, la convención de las columnas ``DateTime`` del repo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _public(row: AccessBootstrap) -> dict:
    return {
        "opened_at": row.opened_at,
        "closes_at": row.closes_at,
        "closed_at": row.closed_at,
        "closed_reason": row.closed_reason,
    }


class AccessBootstrapModel:
    @staticmethod
    def _session():
        return Database().get_declarative_base_session()

    def get(self) -> dict | None:
        """La fila, o ``None`` si todavía no existe. Lanza si la TABLA no existe (ver servicio)."""
        session = self._session()
        try:
            row = session.get(AccessBootstrap, SINGLETON_ID)
            return _public(row) if row else None
        finally:
            session.close()

    def insert(
        self,
        *,
        opened_at: datetime | None,
        closes_at: datetime | None,
        closed_at: datetime | None = None,
        closed_reason: str | None = None,
    ) -> bool:
        """Crea la fila. ``False`` si otra llamada la creó primero (choque en la PK)."""
        now = utcnow()
        session = self._session()
        try:
            session.add(
                AccessBootstrap(
                    id=SINGLETON_ID,
                    opened_at=opened_at,
                    closes_at=closes_at,
                    closed_at=closed_at,
                    closed_reason=closed_reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False
        finally:
            session.close()

    def _update(self, *where, **values) -> bool:
        session = self._session()
        try:
            res = session.execute(
                update(AccessBootstrap)
                .where(AccessBootstrap.id == SINGLETON_ID, *where)
                .values(updated_at=utcnow(), **values)
            )
            session.commit()
            return res.rowcount == 1
        finally:
            session.close()

    def open_pending(self, *, opened_at: datetime, closes_at: datetime) -> bool:
        """Por abrir → abierta. Solo la gana el PRIMER arranque."""
        return self._update(
            AccessBootstrap.opened_at.is_(None),
            AccessBootstrap.closed_at.is_(None),
            opened_at=opened_at,
            closes_at=closes_at,
        )

    def close(self, *, opened_at: datetime, closed_at: datetime, reason: str) -> bool:
        """
        Abierta → cerrada, compare-and-set sobre ``opened_at``: si entretanto una recuperación
        la reabrió (otro ``opened_at``), este cierre perdió y no pisa la ventana nueva.
        """
        return self._update(
            AccessBootstrap.closed_at.is_(None),
            AccessBootstrap.opened_at == opened_at,
            closed_at=closed_at,
            closed_reason=reason,
        )

    def reopen(self, *, opened_at: datetime, closes_at: datetime) -> None:
        """Abre (o reabre) con un plazo nuevo, sea cual sea el estado. Recuperación y siembra."""
        if self._update(
            opened_at=opened_at, closes_at=closes_at, closed_at=None, closed_reason=None
        ):
            return
        if not self.insert(opened_at=opened_at, closes_at=closes_at):
            # Otro arranque la creó entre el UPDATE y el INSERT: se reabre sobre la suya.
            self._update(
                opened_at=opened_at, closes_at=closes_at, closed_at=None, closed_reason=None
            )
