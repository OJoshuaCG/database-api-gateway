"""
Modelo SQL de las capacidades puntuales: persistencia y transiciones de estado.

Las transiciones son COMPARE-AND-SET (D5): ``UPDATE ... WHERE id = :id AND status = :esperado``.
Un ``SELECT`` seguido de un ``UPDATE`` dejaría que dos revocaciones (o una revocación y una
aprobación) concurrentes pisen la misma fila; con el estado en el ``WHERE``, la segunda ve
``rowcount = 0`` y el controller responde 409 sin ``FOR UPDATE``.

Las filas no se borran nunca: toda salida de ``live_key`` pasa por acá y lo apaga junto con el
``status``, porque un ``CHECK`` ata los dos (``live_key_status``).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.capability_grant import CapabilityGrant
from app.models.environment import Environment
from app.models.server import Server
from app.models.user import User
from app.services.capability_catalog import CODE_GRANT_DUPLICATE

#: Vigencia de una solicitud pendiente (R3).
PENDING_TTL = timedelta(days=7)


def utcnow() -> datetime:
    """UTC naive, la convención de las columnas ``DateTime`` del repo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _public(row: CapabilityGrant) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "capability": row.capability,
        "scope_type": row.scope_type,
        "scope_id": row.scope_id,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "expires_at": row.expires_at,
        "request_reason": row.request_reason,
        "decision_reason": row.decision_reason,
    }


class CapabilityGrantModel:
    @staticmethod
    def _session():
        return Database().get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def scope_name(self, scope_type: str, scope_id: int) -> str | None:
        """Nombre del entorno/servidor, o ``None`` si NO EXISTE (valida la existencia)."""
        return self.scope_names([(scope_type, scope_id)]).get((scope_type, scope_id))

    def scope_names(self, keys: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """``{(scope_type, scope_id): nombre}`` en dos consultas como máximo (sin N+1)."""
        out: dict[tuple[str, int], str] = {}
        env_ids = {i for t, i in keys if t == "environment"}
        srv_ids = {i for t, i in keys if t == "server"}
        session = self._session()
        try:
            if env_ids:
                for i, name in session.execute(
                    select(Environment.id, Environment.name).where(Environment.id.in_(env_ids))
                ):
                    out[("environment", i)] = name
            if srv_ids:
                for i, name in session.execute(
                    select(Server.id, Server.name).where(Server.id.in_(srv_ids))
                ):
                    out[("server", i)] = name
        finally:
            session.close()
        return out

    def usernames(self, ids: set[int]) -> dict[int, str]:
        """``{user_id: username}`` en una sola consulta."""
        if not ids:
            return {}
        session = self._session()
        try:
            return dict(
                session.execute(select(User.id, User.username).where(User.id.in_(ids))).all()
            )
        finally:
            session.close()

    def get(self, grant_id: int) -> dict | None:
        session = self._session()
        try:
            row = session.get(CapabilityGrant, grant_id)
            return _public(row) if row else None
        finally:
            session.close()

    def list_for_user(self, user_id: int, status: str | None = None) -> list[dict]:
        session = self._session()
        try:
            stmt = select(CapabilityGrant).where(CapabilityGrant.user_id == user_id)
            if status:
                stmt = stmt.where(CapabilityGrant.status == status)
            stmt = stmt.order_by(CapabilityGrant.id.desc())
            return [_public(r) for r in session.scalars(stmt).all()]
        finally:
            session.close()

    def list_pending(self) -> list[dict]:
        """Solicitudes pendientes y NO vencidas (la bandeja), de la más vieja a la más nueva."""
        session = self._session()
        try:
            stmt = (
                select(CapabilityGrant)
                .where(
                    CapabilityGrant.status == "pending",
                    CapabilityGrant.expires_at > utcnow(),
                )
                .order_by(CapabilityGrant.id.asc())
            )
            return [_public(r) for r in session.scalars(stmt).all()]
        finally:
            session.close()

    def list_overdue(self) -> list[dict]:
        """Pendientes cuyo ``expires_at`` ya pasó: candidatas a ``expired`` (D6)."""
        session = self._session()
        try:
            stmt = select(CapabilityGrant).where(
                CapabilityGrant.status == "pending",
                CapabilityGrant.expires_at <= utcnow(),
            )
            return [_public(r) for r in session.scalars(stmt).all()]
        finally:
            session.close()

    def list_pending_requested_by(self, user_id: int) -> list[dict]:
        """Pendientes que pidió ``user_id`` (vencidas o no): se cancelan si pierde el rol."""
        session = self._session()
        try:
            stmt = select(CapabilityGrant).where(
                CapabilityGrant.status == "pending",
                CapabilityGrant.requested_by == user_id,
            )
            return [_public(r) for r in session.scalars(stmt).all()]
        finally:
            session.close()

    def find_live(
        self, user_id: int, capability: str, scope_type: str, scope_id: int
    ) -> dict | None:
        session = self._session()
        try:
            row = session.scalars(
                select(CapabilityGrant).where(
                    CapabilityGrant.user_id == user_id,
                    CapabilityGrant.capability == capability,
                    CapabilityGrant.scope_type == scope_type,
                    CapabilityGrant.scope_id == scope_id,
                    CapabilityGrant.live_key == 1,
                )
            ).first()
            return _public(row) if row else None
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura                                                          #
    # ------------------------------------------------------------------ #
    def insert(
        self,
        *,
        user_id: int,
        capability: str,
        scope_type: str,
        scope_id: int,
        requested_by: int | None,
        pending: bool,
        reason: str | None,
    ) -> dict:
        """
        Inserta la fila. ``pending`` (capacidad sensible) vence a los 7 días; el resto nace
        ``active``. El ``UNIQUE`` es el respaldo ante dos inserts concurrentes: el perdedor recibe
        ``IntegrityError`` y se mapea al mismo 409 que el chequeo previo.
        """
        now = utcnow()
        session = self._session()
        try:
            row = CapabilityGrant(
                user_id=user_id,
                capability=capability,
                scope_type=scope_type,
                scope_id=scope_id,
                status="pending" if pending else "active",
                live_key=1,
                requested_by=requested_by,
                requested_at=now,
                expires_at=(now + PENDING_TTL) if pending else None,
                request_reason=reason,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe una capacidad puntual viva igual para esta persona.",
                    status_code=409,
                    public_context={"code": CODE_GRANT_DUPLICATE},
                ) from exc
            session.refresh(row)
            return _public(row)
        finally:
            session.close()

    def close_live(
        self, grant_id: int, *, expected_status: str, new_status: str, decided_by: int | None,
        reason: str | None = None,
    ) -> bool:
        """
        Compare-and-set de una fila VIVA a un estado terminal. ``True`` si esta llamada ganó.

        Apaga ``live_key`` junto con ``status`` (el ``CHECK`` los ata). ``False`` significa que
        alguien más la cambió antes: el controller relee y responde 409.
        """
        session = self._session()
        try:
            res = session.execute(
                update(CapabilityGrant)
                .where(
                    CapabilityGrant.id == grant_id,
                    CapabilityGrant.status == expected_status,
                    CapabilityGrant.live_key == 1,
                )
                .values(
                    status=new_status,
                    live_key=None,
                    decided_by=decided_by,
                    decided_at=utcnow(),
                    decision_reason=reason,
                )
            )
            session.commit()
            return res.rowcount == 1
        finally:
            session.close()

    def decide_pending(
        self, grant_id: int, *, approve: bool, decided_by: int | None, reason: str | None
    ) -> bool:
        """
        Compare-and-set de una solicitud PENDIENTE y NO VENCIDA (D5). ``True`` si esta llamada
        ganó; con dos aprobadores simultáneos solo uno ve ``rowcount = 1``.

        Aprobar deja ``live_key = 1`` y BORRA ``expires_at``: el lector de capacidades activas
        descarta toda fila con ``expires_at`` vencido, así que conservar los 7 días de la
        solicitud haría caducar la capacidad recién aprobada. Rechazar apaga ``live_key`` (el
        ``CHECK`` lo ata al estado terminal).
        """
        values = {
            "status": "active" if approve else "rejected",
            "live_key": 1 if approve else None,
            "decided_by": decided_by,
            "decided_at": utcnow(),
            "decision_reason": reason,
        }
        if approve:
            values["expires_at"] = None
        session = self._session()
        try:
            res = session.execute(
                update(CapabilityGrant)
                .where(
                    CapabilityGrant.id == grant_id,
                    CapabilityGrant.status == "pending",
                    CapabilityGrant.live_key == 1,
                    CapabilityGrant.expires_at > utcnow(),
                )
                .values(**values)
            )
            session.commit()
            return res.rowcount == 1
        finally:
            session.close()
