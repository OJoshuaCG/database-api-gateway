"""
Modelo SQL de las capacidades puntuales: persistencia y transiciones de estado.

Las transiciones son COMPARE-AND-SET (D5): ``UPDATE ... WHERE id = :id AND status = :esperado``.
Un ``SELECT`` seguido de un ``UPDATE`` dejaría que dos revocaciones (o una revocación y una
aprobación) concurrentes pisen la misma fila; con el estado en el ``WHERE``, la segunda ve
``rowcount = 0`` y el controller responde 409 sin ``FOR UPDATE``.

Las filas no se borran nunca: toda salida de ``live_key`` pasa por acá y lo apaga junto con el
``status``, porque un ``CHECK`` ata los dos (``live_key_status``).
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.access_grant import AccessGrant
from app.models.capability_grant import CapabilityGrant
from app.models.environment import Environment
from app.models.server import Server
from app.models.user import User
from app.services.capability_catalog import CODE_GRANT_DUPLICATE, CODE_SCOPE_HAS_GRANTS

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
        "sod_override": _override(row.sod_override_json),
    }


def _override(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def assert_scope_has_no_grants(session, scope_type: str, scope_id: int) -> None:
    """
    409 ``access.scope_has_grants`` si algún acceso apunta todavía a este entorno/servidor.

    ``scope_id`` no tiene FK (es polimórfico: entorno o servidor), así que el motor no impide
    borrar el destino. Sin este chequeo el grant SOBREVIVE a su destino y se pega al próximo
    objeto que reutilice el id (SQLite reutiliza; MySQL < 8 recalcula ``AUTO_INCREMENT`` tras
    un reinicio): un ``viewer`` en el entorno 3 borrado pasaría a regir sobre un entorno 3 nuevo
    que nadie le otorgó. Se rechaza en vez de borrar en cascada porque quitar accesos es una
    decisión de quien administra accesos, no un efecto colateral de ordenar el inventario.

    Cuenta ``access_grants`` y las capacidades puntuales VIVAS (``pending``/``active``, las que
    tienen ``live_key``); las decididas, revocadas o vencidas son historia y no bloquean. Corre
    en la ``session`` del borrado, justo antes del ``DELETE``.
    """
    roles = (
        session.query(func.count(AccessGrant.id))
        .filter(AccessGrant.scope_type == scope_type, AccessGrant.scope_id == scope_id)
        .scalar()
        or 0
    )
    puntuales = (
        session.query(func.count(CapabilityGrant.id))
        .filter(
            CapabilityGrant.scope_type == scope_type,
            CapabilityGrant.scope_id == scope_id,
            CapabilityGrant.live_key.is_not(None),
        )
        .scalar()
        or 0
    )
    if roles or puntuales:
        destino = "El entorno" if scope_type == "environment" else "El servidor"
        raise AppHttpException(
            message=(
                f"{destino} tiene accesos otorgados ({roles} por alcance, {puntuales} "
                "capacidades puntuales vivas). Quitalos o revocalos antes de borrarlo."
            ),
            status_code=409,
            public_context={
                "code": CODE_SCOPE_HAS_GRANTS,
                "access_grant_count": roles,
                "capability_grant_count": puntuales,
            },
        )


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

    def list_live_for_user(self, user_id: int) -> list[dict]:
        """``pending`` y ``active`` de UNA persona (lo que ``/auth/me`` muestra de sí misma)."""
        session = self._session()
        try:
            stmt = (
                select(CapabilityGrant)
                .where(
                    CapabilityGrant.user_id == user_id,
                    CapabilityGrant.status.in_(("pending", "active")),
                )
                .order_by(CapabilityGrant.id.asc())
            )
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
        sod_override: dict | None = None,
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
                sod_override_json=(
                    json.dumps(sod_override, ensure_ascii=False) if sod_override else None
                ),
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

    def insert_many(
        self,
        *,
        user_id: int,
        capability: str,
        scope_type: str,
        scope_ids: list[int],
        requested_by: int | None,
        pending: bool,
        reason: str | None,
        sod_override: dict | None = None,
    ) -> list[dict]:
        """
        ``insert`` de VARIOS destinos en UNA transacción: o nacen todas o ninguna. Si el
        ``UNIQUE`` hace perder a cualquiera (alta concurrente), se revierte el lote entero y se
        responde el mismo 409 que el chequeo previo.
        """
        now = utcnow()
        session = self._session()
        try:
            rows = [
                CapabilityGrant(
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
                    sod_override_json=(
                        json.dumps(sod_override, ensure_ascii=False) if sod_override else None
                    ),
                )
                for scope_id in scope_ids
            ]
            session.add_all(rows)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe una capacidad puntual viva igual para esta persona.",
                    status_code=409,
                    public_context={"code": CODE_GRANT_DUPLICATE},
                ) from exc
            for row in rows:
                session.refresh(row)
            return [_public(row) for row in rows]
        finally:
            session.close()

    def live_scope_ids(
        self, user_id: int, capability: str, scope_type: str, scope_ids: list[int]
    ) -> set[int]:
        """Cuáles de ``scope_ids`` ya tienen una capacidad viva igual (una consulta)."""
        session = self._session()
        try:
            return set(
                session.scalars(
                    select(CapabilityGrant.scope_id).where(
                        CapabilityGrant.user_id == user_id,
                        CapabilityGrant.capability == capability,
                        CapabilityGrant.scope_type == scope_type,
                        CapabilityGrant.scope_id.in_(scope_ids),
                        CapabilityGrant.live_key == 1,
                    )
                ).all()
            )
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
