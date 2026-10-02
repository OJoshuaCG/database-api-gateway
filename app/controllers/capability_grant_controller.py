"""
Controller de las CAPACIDADES PUNTUALES: crear, listar y revocar (B3).

Quién puede qué (decisiones de negocio, no negociables acá)
-----------------------------------------------------------
- Solo ``access_admin`` actúa (D10). Las rutas siguen declarando ``gateway.admin`` —que también
  tiene ``security_officer``—, así que el controller exige además la global ``access_admin`` y
  responde un 403 opaco (``access.forbidden``) si falta.
- Nadie se otorga ni se revoca capacidades a sí mismo (``access.self_modification_forbidden``).
- Una capacidad puntual SUMA al rol y nunca es de eje global (``is_grantable``).
- Techo: quien otorga tiene que tener la capacidad EN ese alcance (rol del alcance ∪ globales ∪
  sus propias capacidades puntuales; las lecturas implícitas cuentan).
- Las 7 capacidades sensibles nacen ``pending`` y no surten efecto hasta que un SEGUNDO
  access_admin las apruebe (la aprobación es del B4). El resto nace ``active``.
- Revocar lo hace un solo access_admin, sin techo: activa → ``revoked``, pendiente → ``cancelled``.

Auditoría (D11): ``audit.record`` con ``privilege=capacidad``, ``grantee=username``,
``object_level=scope_type``, ``object_name="environment:3"`` y un ``detail`` JSON con el diff de
estado. Los rechazos por auto-otorgamiento y techo se auditan como ``failure``.
"""

import json

from app.core.actor import Actor, identity_of
from app.core.capability_resolution import capability_at_point
from app.core.scope import ScopePoint, resolve_environment_id
from app.controllers.gateway_user_controller import CODE_NOT_FOUND
from app.exceptions import AppHttpException
from app.models.capability_grant_model import CapabilityGrantModel
from app.models.user_model import UserModel
from app.services import audit
from app.services.capability_catalog import (
    CODE_CAPABILITY_NOT_GRANTABLE,
    CODE_FORBIDDEN,
    CODE_GRANT_CEILING,
    CODE_GRANT_DUPLICATE,
    CODE_GRANT_NOT_FOUND,
    CODE_GRANT_NOT_PENDING,
    CODE_GRANT_SCOPE_NOT_FOUND,
    CODE_GRANT_USER_INACTIVE,
    CODE_SELF_MODIFICATION,
    IMPLIED_READ,
    Capability,
    GlobalCapability,
    is_grantable,
    is_sensitive,
)


def assert_access_admin(actor) -> None:
    """403 opaco si el actor no tiene la global ``access_admin`` (D10). No dice qué falta."""
    if not (
        isinstance(actor, Actor)
        and actor.kind == "admin"
        and GlobalCapability.ACCESS_ADMIN in actor.global_capabilities
    ):
        raise AppHttpException(
            message="No tienes permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_FORBIDDEN},
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

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_for_user(self, user_id: int, actor, status: str | None = None) -> list[dict]:
        assert_access_admin(actor)
        self._user_or_404(user_id)
        return self._serialize_many(self.grants.list_for_user(user_id, status))

    # ------------------------------------------------------------------ #
    # Alta                                                               #
    # ------------------------------------------------------------------ #
    def create(self, user_id: int, data: dict, actor) -> dict:
        """
        Orden de los chequeos (el primero que falla gana, y los más baratos y menos
        informativos van antes): access_admin (403) → auto-otorgamiento (409) → usuario existe
        (404) y activo (409) → otorgable (422) → alcance existe (404) → techo (409) → duplicado
        (409, con el ``UNIQUE`` de respaldo).
        """
        assert_access_admin(actor)
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
        assert_access_admin(actor)
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
