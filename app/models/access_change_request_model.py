"""
Modelo SQL de las solicitudes de elevación (``access_change_requests``): persistencia y
transiciones de estado.

Toda transición es COMPARE-AND-SET (``UPDATE ... WHERE id = :id AND status = 'pending'``), igual
que las capacidades puntuales: con dos aprobadores simultáneos gana uno solo y el otro ve
``rowcount = 0``. La aprobación además reclama la fila DENTRO de la transacción que escribe el
acceso (``claim_in``), así que "aplicada" y "acceso escrito" no pueden divergir: si el reclamo
pierde, la escritura se deshace; si la escritura falla, el reclamo también.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update

from app.core.database import Database
from app.models.access_change_request import STATUS_PENDING, AccessChangeRequest

#: Vigencia de una solicitud pendiente: 7 días, igual que las capacidades puntuales.
PENDING_TTL = timedelta(days=7)


def utcnow() -> datetime:
    """UTC naive, la convención de las columnas ``DateTime`` del repo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _public(row: AccessChangeRequest) -> dict:
    try:
        desired = json.loads(row.desired_state_json or "{}")
    except ValueError:
        desired = {}
    return {
        "id": row.id,
        "target_user_id": row.target_user_id,
        "requested_by": row.requested_by,
        "desired": desired,
        "before_hash": row.before_hash,
        "status": row.status,
        "expires_at": row.expires_at,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "reason": row.reason,
        "created_at": row.created_at,
    }


class AccessChangeRequestModel:
    @staticmethod
    def _session():
        return Database().get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def get(self, request_id: int) -> dict | None:
        session = self._session()
        try:
            row = session.get(AccessChangeRequest, request_id)
            return _public(row) if row else None
        finally:
            session.close()

    def _list(self, *where) -> list[dict]:
        session = self._session()
        try:
            stmt = select(AccessChangeRequest).where(*where).order_by(AccessChangeRequest.id.asc())
            return [_public(r) for r in session.scalars(stmt).all()]
        finally:
            session.close()

    def list_pending(self) -> list[dict]:
        """Pendientes NO vencidas (la bandeja), de la más vieja a la más nueva."""
        return self._list(
            AccessChangeRequest.status == STATUS_PENDING,
            AccessChangeRequest.expires_at > utcnow(),
        )

    def list_overdue(self) -> list[dict]:
        return self._list(
            AccessChangeRequest.status == STATUS_PENDING,
            AccessChangeRequest.expires_at <= utcnow(),
        )

    def list_pending_for_target(self, user_id: int) -> list[dict]:
        return self._list(
            AccessChangeRequest.status == STATUS_PENDING,
            AccessChangeRequest.target_user_id == user_id,
        )

    def list_pending_requested_by(self, user_id: int) -> list[dict]:
        return self._list(
            AccessChangeRequest.status == STATUS_PENDING,
            AccessChangeRequest.requested_by == user_id,
        )

    # ------------------------------------------------------------------ #
    # Escritura                                                          #
    # ------------------------------------------------------------------ #
    def insert(
        self, *, target_user_id: int, requested_by: int | None, desired: dict, before_hash: str
    ) -> dict:
        now = utcnow()
        session = self._session()
        try:
            row = AccessChangeRequest(
                target_user_id=target_user_id,
                requested_by=requested_by,
                desired_state_json=json.dumps(desired, ensure_ascii=False, sort_keys=True),
                before_hash=before_hash,
                status=STATUS_PENDING,
                expires_at=now + PENDING_TTL,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _public(row)
        finally:
            session.close()

    def close_pending(
        self,
        request_id: int,
        *,
        new_status: str,
        decided_by: int | None,
        reason: str | None,
        only_live: bool = False,
    ) -> bool:
        """
        ``pending`` → ``new_status`` (terminal). ``True`` si esta llamada ganó. ``only_live``
        exige además que no esté vencida (rechazar o cancelar una vencida es ``not_pending``).
        """
        now = utcnow()
        cond = [AccessChangeRequest.id == request_id, AccessChangeRequest.status == STATUS_PENDING]
        if only_live:
            cond.append(AccessChangeRequest.expires_at > now)
        session = self._session()
        try:
            res = session.execute(
                update(AccessChangeRequest)
                .where(*cond)
                .values(
                    status=new_status,
                    decided_by=decided_by,
                    decided_at=now,
                    reason=reason,
                    updated_at=now,
                )
            )
            session.commit()
            return res.rowcount == 1
        finally:
            session.close()

    @staticmethod
    def claim_in(conn, request_id: int, *, decided_by: int | None, reason: str | None) -> bool:
        """
        Reclama la solicitud como ``applied`` DENTRO de ``conn`` (la transacción que escribe el
        acceso). Compare-and-set sobre ``pending`` y no vencida. ``False`` = otro la decidió o
        venció: el llamador aborta y la transacción entera se deshace.
        """
        now = utcnow()
        res = conn.execute(
            text(
                "UPDATE access_change_requests SET status = 'applied', decided_by = :d, "
                "decided_at = :n, reason = :r, updated_at = :n "
                "WHERE id = :id AND status = 'pending' AND expires_at > :n"
            ),
            {"d": decided_by, "n": now, "r": reason, "id": request_id},
        )
        return res.rowcount == 1
