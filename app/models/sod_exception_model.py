"""
Modelo SQL de las excepciones a la separación de deberes (``sod_exceptions``).

``live_rules`` está en el camino de CADA request autenticada de una cuenta con
``security_officer`` y falla CERRADO: sin la tabla (código antes que la migración, un downgrade)
devuelve ``[]``, así que el lector descarta ``security_officer`` de toda cuenta combinada en vez
de tumbar la autenticación o, peor, dejarla pasar sin excepción.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.database import Database
from app.models.sod_exception import CLOSED_RESOLVED, GRANDFATHERED_REASON, SodException
from app.models.user import User

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """UTC naive, la convención de las columnas ``DateTime`` del repo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _live(now: datetime):
    return (
        SodException.closed_at.is_(None),
        or_(SodException.expires_at.is_(None), SodException.expires_at > now),
    )


def _public(row: SodException) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "rule": row.rule,
        "reason": row.reason,
        "requested_by": row.requested_by,
        "approved_by": row.approved_by,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "closed_at": row.closed_at,
        "closed_reason": row.closed_reason,
        "grandfathered": row.expires_at is None and row.reason == GRANDFATHERED_REASON,
    }


class SodExceptionModel:
    @staticmethod
    def _session():
        return Database().get_declarative_base_session()

    def live_rules(self, user_id: int) -> list[str]:
        """Reglas con una excepción VIVA para ``user_id``. Fail-closed sin la tabla (ver módulo)."""
        try:
            session = self._session()
            try:
                stmt = select(SodException.rule).where(
                    SodException.user_id == user_id, *_live(utcnow())
                )
                return sorted({r for (r,) in session.execute(stmt).all()})
            finally:
                session.close()
        except (ProgrammingError, OperationalError):
            logger.error(
                "sod_exceptions no está disponible: ninguna combinación queda exceptuada",
                exc_info=True,
            )
            return []

    def list_live(self, user_id: int | None = None) -> list[dict]:
        """Excepciones VIVAS (de una cuenta o de todas), de la más vieja a la más nueva."""
        session = self._session()
        try:
            stmt = select(SodException).where(*_live(utcnow()))
            if user_id is not None:
                stmt = stmt.where(SodException.user_id == user_id)
            stmt = stmt.order_by(SodException.id.asc())
            return [_public(r) for r in session.scalars(stmt).all()]
        finally:
            session.close()

    def insert(
        self,
        *,
        user_id: int,
        rule: str,
        reason: str,
        requested_by: int | None,
        expires_at: datetime | None,
    ) -> dict:
        session = self._session()
        try:
            now = utcnow()
            row = SodException(
                user_id=user_id,
                rule=rule,
                reason=reason,
                requested_by=requested_by,
                approved_by=None,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _public(row)
        finally:
            session.close()

    def grandfather(self, user_id: int, rules: list[str]) -> list[dict]:
        """
        Inserta una fila HEREDADA por cada regla de ``rules`` que todavía no tenga una viva.
        Idempotente: correrlo dos veces no duplica.
        """
        vivas = set(self.live_rules(user_id))
        return [
            self.insert(
                user_id=user_id,
                rule=rule,
                reason=GRANDFATHERED_REASON,
                requested_by=None,
                expires_at=None,
            )
            for rule in rules
            if rule not in vivas
        ]

    def close_live(self, user_id: int, rules: list[str], *, reason: str = CLOSED_RESOLVED) -> int:
        """Cierra las excepciones vivas de ``user_id`` para ``rules``. Devuelve cuántas cerró."""
        if not rules:
            return 0
        session = self._session()
        try:
            now = utcnow()
            res = session.execute(
                update(SodException)
                .where(
                    SodException.user_id == user_id,
                    SodException.rule.in_(rules),
                    *_live(now),
                )
                .values(closed_at=now, closed_reason=reason, updated_at=now)
            )
            session.commit()
            return res.rowcount or 0
        finally:
            session.close()

    def users_with_security_officer(self) -> list[dict]:
        """``[{id, username, is_active}]`` de toda cuenta con ``security_officer``."""
        from app.models.access_grant import UserGlobalCapability

        session = self._session()
        try:
            stmt = (
                select(User.id, User.username, User.is_active)
                .join(UserGlobalCapability, UserGlobalCapability.user_id == User.id)
                .where(UserGlobalCapability.capability == "security_officer")
                .order_by(User.id.asc())
            )
            return [
                {"id": i, "username": u, "is_active": bool(a)}
                for i, u, a in session.execute(stmt).all()
            ]
        finally:
            session.close()
