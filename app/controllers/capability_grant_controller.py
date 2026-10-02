"""
Controller de las CAPACIDADES PUNTUALES: crear, listar y revocar (B3).

Quién puede qué (decisiones de negocio, no negociables acá)
-----------------------------------------------------------
- Solo ``access_admin`` actúa (D10). Lo hace cumplir la RUTA: todas declaran ``access.admin``,
  que solo trae la global ``access_admin`` (invariantes 9 y 10 del catálogo) y que ningún otro
  camino puede acuñar —es de eje global, así que no es otorgable suelta, y no es
  ``agent_allowed``, así que ningún token la tiene—. Mientras la capacidad era ``gateway.admin``
  y también la tenía ``security_officer``, el controller re-exigía la global con un
  ``assert_access_admin``; con la capacidad partida ese chequeo era idéntico al de la ruta y se
  retiró. Si alguna vez se llama a este controller desde una ruta que NO declara
  ``access.admin``, es esa ruta la que está mal.
- Nadie se otorga ni se revoca capacidades a sí mismo (``access.self_modification_forbidden``).
- Una capacidad puntual SUMA al rol y nunca es de eje global (``is_grantable``).
- Techo: quien otorga tiene que tener la capacidad EN ese alcance (rol del alcance ∪ globales ∪
  sus propias capacidades puntuales; las lecturas implícitas cuentan).
- Las 7 capacidades sensibles nacen ``pending`` y no surten efecto hasta que un SEGUNDO
  access_admin las apruebe (la aprobación es del B4). El resto nace ``active``.
- Revocar lo hace un solo access_admin, sin techo: activa → ``revoked``, pendiente → ``cancelled``.

Aprobación (B4)
---------------
- Aprueba OTRO access_admin: ni quien la pidió (``access.self_approval_forbidden``) ni la propia
  persona destino (``access.self_modification_forbidden``).
- Solo se re-verifica el techo de quien APRUEBA (tiene que tener la capacidad en ese alcance); del
  solicitante solo se exige que siga siendo un access_admin activo. Si dejó de serlo, la solicitud
  se cancela y se responde 409.
- La decisión es compare-and-set (``status='pending' AND expires_at > now``): con dos aprobadores
  simultáneos gana uno solo y el otro recibe ``access.grant_not_pending``.
- Vencimiento perezoso (D6): ``expire_overdue`` corre en cada lectura/decisión y una vez al
  arrancar. Los eventos automáticos (vencida, cancelada por pérdida de rol) se auditan con
  ``actor_type="system"``.

Auditoría (D11): ``audit.record`` con ``privilege=capacidad``, ``grantee=username``,
``object_level=scope_type``, ``object_name="environment:3"`` y un ``detail`` JSON con el diff de
estado. Los rechazos por auto-otorgamiento y techo se auditan como ``failure``.
"""

import json

from app.core.actor import identity_of
from app.core.capability_resolution import capability_at_point
from app.core.scope import ScopePoint, resolve_environment_id
from app.controllers.gateway_user_controller import CODE_NOT_FOUND
from app.exceptions import AppHttpException
from app.models.capability_grant_model import CapabilityGrantModel
from app.models.user_model import UserModel
from app.services import audit
from app.services.capability_catalog import (
    CODE_CAPABILITY_NOT_GRANTABLE,
    CODE_GRANT_CEILING,
    CODE_GRANT_DUPLICATE,
    CODE_GRANT_NOT_FOUND,
    CODE_GRANT_NOT_PENDING,
    CODE_GRANT_SCOPE_NOT_FOUND,
    CODE_GRANT_USER_INACTIVE,
    CODE_SELF_APPROVAL,
    CODE_SELF_MODIFICATION,
    IMPLIED_READ,
    Capability,
    GlobalCapability,
    is_grantable,
    is_sensitive,
)


def _object_name(scope_type: str, scope_id: int) -> str:
    return f"{scope_type}:{scope_id}"


class CapabilityGrantController:
    def __init__(self):
        self.grants = CapabilityGrantModel()
        self.users = UserModel()

    # ------------------------------------------------------------------ #
    # Serialización                                                      #
    # ------------------------------------------------------------------ #
    def _serialize_many(self, rows: list[dict]) -> list[dict]:
        ids = {r["user_id"] for r in rows}
        ids |= {r[k] for r in rows for k in ("requested_by", "decided_by") if r[k] is not None}
        names = self.grants.usernames(ids)
        scopes = self.grants.scope_names([(r["scope_type"], r["scope_id"]) for r in rows])

        def ref(uid):
            return {"id": uid, "username": names.get(uid, "")} if uid is not None else None

        out = []
        for r in rows:
            cap = r["capability"]
            implied = IMPLIED_READ.get(Capability(cap), frozenset()) if is_grantable(cap) else ()
            out.append(
                {
                    **r,
                    "username": names.get(r["user_id"]),
                    "scope_name": scopes.get((r["scope_type"], r["scope_id"])),
                    "sensitive": is_sensitive(cap),
                    "requested_by": ref(r["requested_by"]),
                    "decided_by": ref(r["decided_by"]),
                    "implies": sorted(c.value for c in implied),
                }
            )
        return out

    def _serialize(self, row: dict) -> dict:
        return self._serialize_many([row])[0]

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _user_or_404(self, user_id: int) -> dict:
        fila = self.users.find_by_id(user_id)
        if not fila:
            raise AppHttpException(
                message="Usuario del gateway no encontrado.",
                status_code=404,
                public_context={"code": CODE_NOT_FOUND},
                context={"user_id": user_id},
            )
        return fila

    @staticmethod
    def _is_self(actor, user_id: int) -> bool:
        actor_id, _ = identity_of(actor)
        return actor_id is not None and int(actor_id) == int(user_id)

    @staticmethod
    def _self_error(action: str) -> AppHttpException:
        return AppHttpException(
            message=(
                f"No puedes {action}: esa operación la tiene que hacer otra persona con "
                "permiso de administración de accesos."
            ),
            status_code=409,
            public_context={"code": CODE_SELF_MODIFICATION},
        )

    @staticmethod
    def _point_for(scope_type: str, scope_id: int) -> ScopePoint:
        """
        El punto contra el que se mide el techo. Entorno: ``(E, None)``. Servidor: el entorno
        MÁS PROTEGIDO de sus bases (fail-closed, como la capa 2) y el id del servidor.
        """
        if scope_type == "environment":
            return ScopePoint(environment_id=scope_id, server_id=None)
        env_id = resolve_environment_id(server_id=scope_id, managed_database_id=None)
        return ScopePoint(environment_id=env_id, server_id=scope_id)

    def _audit(
        self,
        action: str,
        actor,
        grantee: str,
        capability: str,
        scope_type: str,
        scope_id: int,
        *,
        grant_id: int | None,
        before: str | None,
        after: str | None,
        status: str = "success",
        reason: str | None = None,
    ) -> None:
        detail = {"before": {"status": before}, "after": {"status": after}, "grant_id": grant_id}
        if reason:
            detail["reason"] = reason
        audit.record(
            action,
            status=status,
            admin=actor,
            target_type="capability_grant",
            target_id=grant_id,
            touched_engine=False,
            grantee=grantee,
            privilege=capability,
            object_level=scope_type,
            object_name=_object_name(scope_type, scope_id),
            detail=json.dumps(detail, ensure_ascii=False),
        )

    def _audit_system(self, action: str, row: dict, *, before: str, after: str,
                      reason: str) -> None:
        """Evento automático: sin persona, ``actor_type='system'`` (D11)."""
        names = self.grants.usernames({row["user_id"]})
        detail = {"before": {"status": before}, "after": {"status": after},
                  "grant_id": row["id"], "reason": reason}
        audit.record(
            action,
            admin=None,
            actor_type="system",
            target_type="capability_grant",
            target_id=row["id"],
            touched_engine=False,
            grantee=names.get(row["user_id"]),
            privilege=row["capability"],
            object_level=row["scope_type"],
            object_name=_object_name(row["scope_type"], row["scope_id"]),
            detail=json.dumps(detail, ensure_ascii=False),
        )

    # ------------------------------------------------------------------ #
    # Vencimiento y cancelación automáticos                               #
    # ------------------------------------------------------------------ #
    def expire_overdue(self) -> int:
        """
        Pasa a ``expired`` las pendientes vencidas (D6). Compare-and-set por fila: si otro
        request la aprobó o canceló entre el SELECT y el UPDATE, no se pisa. Devuelve cuántas
        venció ESTA llamada (y solo esas se auditan).
        """
        n = 0
        for row in self.grants.list_overdue():
            if self.grants.close_live(
                row["id"], expected_status="pending", new_status="expired", decided_by=None
            ):
                self._audit_system("capability_grant.expired", row, before="pending",
                                   after="expired", reason="expired")
                n += 1
        return n

    def cancel_pending_requested_by(self, user_id: int, *, reason: str = "requester_lost_access") -> int:
        """
        Cancela las solicitudes pendientes que pidió ``user_id`` (perdió ``access_admin`` o fue
        desactivado). Sin efecto sobre las activas: una aprobación ya dada no depende de quien
        la pidió.
        """
        n = 0
        for row in self.grants.list_pending_requested_by(user_id):
            if self.grants.close_live(
                row["id"], expected_status="pending", new_status="cancelled", decided_by=None
            ):
                self._audit_system("capability_grant.cancelled", row, before="pending",
                                   after="cancelled", reason=reason)
                n += 1
        return n

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_for_user(self, user_id: int, actor, status: str | None = None) -> list[dict]:
        self.expire_overdue()
        self._user_or_404(user_id)
        return self._serialize_many(self.grants.list_for_user(user_id, status))

    # ------------------------------------------------------------------ #
    # Alta                                                               #
    # ------------------------------------------------------------------ #
    def create(self, user_id: int, data: dict, actor) -> dict:
        """
        Orden de los chequeos (el primero que falla gana, y los más baratos y menos
        informativos van antes): ``access.admin`` en la ruta (403) → auto-otorgamiento (409) → usuario existe
        (404) y activo (409) → otorgable (422) → alcance existe (404) → techo (409) → duplicado
        (409, con el ``UNIQUE`` de respaldo).
        """
        capability = data["capability"]
        scope_type, scope_id = data["scope_type"], int(data["scope_id"])
        sensitive = is_sensitive(capability)
        action = "capability_grant.requested" if sensitive else "capability_grant.created"

        user = self._user_or_404(user_id)
        username = user["username"]

        if self._is_self(actor, user_id):
            self._audit(action, actor, username, capability, scope_type, scope_id,
                        grant_id=None, before=None, after=None, status="failure",
                        reason="self_modification_forbidden")
            raise self._self_error("otorgarte capacidades a ti mismo")

        if not user.get("is_active"):
            raise AppHttpException(
                message="La persona está desactivada: reactívala antes de otorgarle capacidades.",
                status_code=409,
                public_context={"code": CODE_GRANT_USER_INACTIVE},
            )

        if not is_grantable(capability):
            raise AppHttpException(
                message="Esa capacidad no se puede otorgar de forma puntual.",
                status_code=422,
                public_context={"code": CODE_CAPABILITY_NOT_GRANTABLE},
            )

        if self.grants.scope_name(scope_type, scope_id) is None:
            raise AppHttpException(
                message="El entorno o servidor indicado no existe.",
                status_code=404,
                public_context={"code": CODE_GRANT_SCOPE_NOT_FOUND},
            )

        point = self._point_for(scope_type, scope_id)
        if not capability_at_point(actor, Capability(capability), point):
            self._audit(action, actor, username, capability, scope_type, scope_id,
                        grant_id=None, before=None, after=None, status="failure",
                        reason="grant_ceiling_exceeded")
            raise AppHttpException(
                message=(
                    "No puedes otorgar más acceso del que tienes en ese alcance. "
                    "Pídeselo a alguien que tenga ese nivel."
                ),
                status_code=409,
                public_context={"code": CODE_GRANT_CEILING},
            )

        if self.grants.find_live(user_id, capability, scope_type, scope_id):
            raise AppHttpException(
                message="Ya existe una capacidad puntual viva igual para esta persona.",
                status_code=409,
                public_context={"code": CODE_GRANT_DUPLICATE},
            )

        actor_id, _ = identity_of(actor)
        row = self.grants.insert(
            user_id=user_id,
            capability=capability,
            scope_type=scope_type,
            scope_id=scope_id,
            requested_by=actor_id,
            pending=sensitive,
            reason=(data.get("reason") or "").strip() or None,
        )
        self._audit(action, actor, username, capability, scope_type, scope_id,
                    grant_id=row["id"], before=None, after=row["status"],
                    reason=row.get("request_reason"))
        return self._serialize(row)

    # ------------------------------------------------------------------ #
    # Revocación                                                         #
    # ------------------------------------------------------------------ #
    def revoke(self, user_id: int, grant_id: int, actor) -> dict:
        """
        Activa → ``revoked``; pendiente → ``cancelled``. Un solo access_admin, sin techo y sin
        segundo aprobador. Una fila de OTRO usuario o inexistente es 404; una ya terminal, 409
        ``grant_not_pending`` (el vocabulario cerrado no tiene un código mejor).
        """
        user = self._user_or_404(user_id)
        if self._is_self(actor, user_id):
            raise self._self_error("revocarte capacidades a ti mismo")

        row = self.grants.get(grant_id)
        if not row or row["user_id"] != user_id:
            raise AppHttpException(
                message="Capacidad puntual no encontrada.",
                status_code=404,
                public_context={"code": CODE_GRANT_NOT_FOUND},
            )

        not_live = AppHttpException(
            message="La capacidad ya no está vigente.",
            status_code=409,
            public_context={"code": CODE_GRANT_NOT_PENDING},
        )
        previous = row["status"]
        if previous not in ("pending", "active"):
            raise not_live
        new_status = "revoked" if previous == "active" else "cancelled"

        actor_id, _ = identity_of(actor)
        if not self.grants.close_live(
            grant_id, expected_status=previous, new_status=new_status, decided_by=actor_id
        ):
            raise not_live

        self._audit(
            f"capability_grant.{new_status}", actor, user["username"], row["capability"],
            row["scope_type"], row["scope_id"], grant_id=grant_id, before=previous,
            after=new_status,
        )
        return self._serialize(self.grants.get(grant_id))

    # ------------------------------------------------------------------ #
    # Aprobación (B4)                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _not_pending() -> AppHttpException:
        return AppHttpException(
            message="La solicitud ya no está pendiente (fue decidida, venció o se canceló).",
            status_code=409,
            public_context={"code": CODE_GRANT_NOT_PENDING},
        )

    def _requester_is_active_access_admin(self, requested_by: int | None) -> bool:
        if requested_by is None:
            return False
        fila = self.users.find_by_id(requested_by)
        if not fila or not fila.get("is_active"):
            return False
        ctx = self.users.find_access_context(requested_by)
        return GlobalCapability.ACCESS_ADMIN.value in (ctx.get("globals") or [])

    def _block_reason(self, actor, row: dict, grantee: dict | None, requester_ok: bool) -> str | None:
        """
        Código ``access.*`` por el que ``actor`` NO puede decidir sobre ``row``, o ``None``.
        Es la única fuente de las reglas de aprobación: ``approve`` y la bandeja la comparten.
        """
        actor_id, _ = identity_of(actor)
        if row["requested_by"] is not None and row["requested_by"] == actor_id:
            return CODE_SELF_APPROVAL
        if row["user_id"] == actor_id:
            return CODE_SELF_MODIFICATION
        if not grantee or not grantee.get("is_active"):
            return CODE_GRANT_USER_INACTIVE
        if not requester_ok:
            return CODE_GRANT_NOT_PENDING
        if self.grants.scope_name(row["scope_type"], row["scope_id"]) is None:
            return CODE_GRANT_SCOPE_NOT_FOUND
        if not is_grantable(row["capability"]) or not capability_at_point(
            actor, Capability(row["capability"]), self._point_for(row["scope_type"], row["scope_id"])
        ):
            return CODE_GRANT_CEILING
        return None

    _BLOCK_MESSAGES = {
        CODE_SELF_APPROVAL: ("No puedes aprobar una solicitud que pediste tú: la tiene que "
                             "aprobar otra persona con permiso de administración de accesos.", 409),
        CODE_SELF_MODIFICATION: ("No puedes aprobarte capacidades a ti mismo.", 409),
        CODE_GRANT_USER_INACTIVE: ("La persona está desactivada: reactívala antes de aprobar.", 409),
        CODE_GRANT_SCOPE_NOT_FOUND: ("El entorno o servidor indicado ya no existe.", 404),
        CODE_GRANT_CEILING: ("No puedes aprobar más acceso del que tienes en ese alcance.", 409),
    }

    def list_pending(self, actor) -> list[dict]:
        """Bandeja: solicitudes pendientes vigentes, cada una con ``can_decide`` para ``actor``."""
        self.expire_overdue()
        rows = self.grants.list_pending()
        out = self._serialize_many(rows)
        requesters: dict = {}
        for raw, ser in zip(rows, out):
            rid = raw["requested_by"]
            if rid not in requesters:
                requesters[rid] = self._requester_is_active_access_admin(rid)
            reason = self._block_reason(
                actor, raw, self.users.find_by_id(raw["user_id"]), requesters[rid]
            )
            ser["can_decide"] = reason is None
            ser["blocked_reason"] = reason
        return out

    def _pending_or_error(self, grant_id: int) -> dict:
        row = self.grants.get(grant_id)
        if not row:
            raise AppHttpException(
                message="Capacidad puntual no encontrada.",
                status_code=404,
                public_context={"code": CODE_GRANT_NOT_FOUND},
            )
        if row["status"] != "pending":
            raise self._not_pending()
        return row

    def approve(self, grant_id: int, actor, reason: str | None = None) -> dict:
        self.expire_overdue()
        row = self._pending_or_error(grant_id)
        grantee = self.users.find_by_id(row["user_id"])
        username = (grantee or {}).get("username", "")

        requester_ok = self._requester_is_active_access_admin(row["requested_by"])
        if not requester_ok and self._self_check(actor, row) is None:
            # Deriva de estado que se coló por los hooks (D7): se cancela y se avisa.
            if self.grants.close_live(grant_id, expected_status="pending",
                                      new_status="cancelled", decided_by=None):
                self._audit_system("capability_grant.cancelled", row, before="pending",
                                   after="cancelled", reason="requester_lost_access")
            raise self._not_pending()

        code = self._block_reason(actor, row, grantee, requester_ok)
        if code is not None:
            self._audit("capability_grant.approved", actor, username, row["capability"],
                        row["scope_type"], row["scope_id"], grant_id=grant_id,
                        before="pending", after="pending", status="failure", reason=code)
            message, status_code = self._BLOCK_MESSAGES.get(
                code, ("No se puede aprobar esta solicitud.", 409)
            )
            raise AppHttpException(message=message, status_code=status_code,
                                   public_context={"code": code})

        actor_id, _ = identity_of(actor)
        reason = (reason or "").strip() or None
        if not self.grants.decide_pending(grant_id, approve=True, decided_by=actor_id,
                                          reason=reason):
            raise self._not_pending()
        self._audit("capability_grant.approved", actor, username, row["capability"],
                    row["scope_type"], row["scope_id"], grant_id=grant_id,
                    before="pending", after="active", reason=reason)
        return self._serialize(self.grants.get(grant_id))

    @staticmethod
    def _self_check(actor, row: dict) -> str | None:
        """Self-approval / self-grantee tienen prioridad sobre el drift del solicitante."""
        actor_id, _ = identity_of(actor)
        if row["requested_by"] is not None and row["requested_by"] == actor_id:
            return CODE_SELF_APPROVAL
        if row["user_id"] == actor_id:
            return CODE_SELF_MODIFICATION
        return None

    def reject(self, grant_id: int, actor, reason: str | None = None) -> dict:
        """Pendiente → ``rejected``. Sin techo ni segundo aprobador: rechazar nunca da acceso."""
        self.expire_overdue()
        row = self._pending_or_error(grant_id)
        grantee = self.users.find_by_id(row["user_id"])
        actor_id, _ = identity_of(actor)
        reason = (reason or "").strip() or None
        if not self.grants.decide_pending(grant_id, approve=False, decided_by=actor_id,
                                          reason=reason):
            raise self._not_pending()
        self._audit("capability_grant.rejected", actor, (grantee or {}).get("username", ""),
                    row["capability"], row["scope_type"], row["scope_id"], grant_id=grant_id,
                    before="pending", after="rejected", reason=reason)
        return self._serialize(self.grants.get(grant_id))
