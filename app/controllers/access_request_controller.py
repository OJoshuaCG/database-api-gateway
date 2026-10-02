"""
Controller de las ELEVACIONES de acceso con segundo aprobador (``/access-requests``, C3).

DE DÓNDE SALE
-------------
El techo de otorgamiento por TENENCIA (``_assert_within_ceiling``: "nunca otorgás más de lo que
tenés") se retiró: obligaba a que quien administra accesos tuviera también cada deber que
reparte, que es justo la combinación que la separación de deberes deshace. Ahora manda la
política de ASIGNACIÓN (``ASSIGNABLE_BY``: ``access_admin`` asigna cualquier rol, global o
capacidad otorgable) y lo que el techo impedía —un administrador solo se crea un títere ``owner``,
recibe su invitación y entra con esa cara (F-2)— lo impide el SEGUNDO APROBADOR: toda elevación
(``needs_second_approver``: ``owner`` base o por alcance, cualquier global, un ``sod_override``)
queda pendiente acá hasta que OTRO ``access_admin`` la apruebe.

``POST /gateway-users``, ``PATCH /gateway-users/{id}`` (rol) y ``PUT /gateway-users/{id}/access``
parten el cambio (``app/core/assignment_policy.split``): lo que no eleva —incluidas SIEMPRE las
bajas— se aplica en el request; lo que eleva nace como solicitud y la respuesta es ``202
access.elevation_pending``. Las capacidades puntuales sensibles tienen su propio flujo pendiente
(``capability_grant_controller``), con las mismas reglas.

REGLAS DE LA APROBACIÓN (las mismas que las capacidades puntuales)
------------------------------------------------------------------
- Aprueba OTRO ``access_admin``: ni quien la pidió (``access.self_approval_forbidden``) ni la
  persona destino (``access.self_modification_forbidden``).
- Quien la pidió tiene que seguir siendo un ``access_admin`` activo; si no, la solicitud se
  cancela (``requester_lost_access``) y se responde 409 ``access.request_not_pending``.
- Si el acceso de la persona cambió desde que se pidió (``before_hash``), la solicitud se cancela
  (``stale``) y se responde 409 ``access.request_stale``: aprobarla pisaría un cambio que nadie
  revisó junto con ella.
- Se re-chequea la separación de deberes (C2) sobre el estado final: entre el pedido y la
  aprobación pudo vencer una excepción o llegar una capacidad puntual.
- La decisión es compare-and-set, y el reclamo corre DENTRO de la transacción que escribe el
  acceso (``replace_access(before_write=...)``), que conserva el candado del último administrador
  (F-24). Al aplicar se tachan las sesiones de la persona.
- Vencimiento perezoso a los 7 días (``expire_overdue`` en cada lectura/decisión y al arrancar).
- Una solicitud nueva sobre la misma persona REEMPLAZA a la pendiente anterior (``superseded``):
  la anterior quedaría vieja igual en cuanto se aplique la parte inmediata de la nueva.

``ACCESS_FOUR_EYES=False`` (instalaciones de un solo administrador): las elevaciones se aplican en
el acto y cada una se audita ``access.elevation_unapproved``. El arranque avisa.

VENTANA DE ARRANQUE (C4): con los cuatro ojos prendidos, mientras la ventana está abierta y quien
pide es el ÚNICO ``access_admin`` activo con credencial, sus elevaciones también se aplican en el
acto, auditadas ``access.bootstrap_assignment`` (``app/services/bootstrap_window.py``). Es lo que
deja a una instalación nueva crear su segundo ``access_admin``. ``decide`` es el único lugar que
elige entre las tres salidas.
"""

from __future__ import annotations

import json

from app.core.actor import Actor, identity_of
from app.core.assignment_policy import AccessState, state_hash
from app.core.environments import ACCESS_FOUR_EYES
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.access_change_request_model import AccessChangeRequestModel
from app.models.user_model import UserModel
from app.services import audit, sod_service
from app.services.capability_catalog import (
    CODE_ELEVATION_PENDING,
    CODE_GRANT_USER_INACTIVE,
    CODE_NOT_ASSIGNABLE,
    CODE_REQUEST_NOT_FOUND,
    CODE_REQUEST_NOT_PENDING,
    CODE_REQUEST_NOT_REQUESTER,
    CODE_REQUEST_STALE,
    CODE_SELF_APPROVAL,
    CODE_SELF_MODIFICATION,
    CODE_SOD_CONFLICT,
    GlobalCapability,
    can_assign,
)

logger = get_logger(__name__)

ACTION_CREATED = "access_request.created"
ACTION_APPROVED = "access_request.approved"
ACTION_REJECTED = "access_request.rejected"
ACTION_CANCELLED = "access_request.cancelled"
ACTION_EXPIRED = "access_request.expired"
ACTION_UNAPPROVED = "access.elevation_unapproved"

ORIGINS = ("create", "update", "set_access")


#: Salidas de ``decide``. ``None`` = no hay nada que eleve.
MODE_WAIT = "wait"  # queda pendiente de un segundo access_admin
MODE_BOOTSTRAP = "bootstrap"  # ventana de arranque: se aplica ya, access.bootstrap_assignment
MODE_UNAPPROVED = "unapproved"  # ACCESS_FOUR_EYES=False: se aplica ya, access.elevation_unapproved


def four_eyes() -> bool:
    """El valor vigente de ``ACCESS_FOUR_EYES``. Los tests lo cambian con ``monkeypatch``."""
    return bool(ACCESS_FOUR_EYES)


def decide(elevations: list[dict], plan, actor) -> str | None:
    """
    Qué pasa con la parte que eleva (elevaciones u override) de un cambio pedido por ``actor``.

    Se decide UNA vez por request y el resultado viaja hasta la auditoría (``record_unapproved``):
    re-evaluar la ventana al auditar podría contar una historia distinta de la que se aplicó.
    """
    if not (elevations or plan):
        return None
    if not four_eyes():
        return MODE_UNAPPROVED
    from app.services import bootstrap_window

    if bootstrap_window.applies_to(actor):
        return MODE_BOOTSTRAP
    return MODE_WAIT


def actor_can_assign(actor, elevations: list[dict]) -> bool:
    """¿La función de ``actor`` asigna TODO lo de ``elevations``? Fail-closed sin ``Actor``."""
    if not isinstance(actor, Actor):
        return False
    globales = actor.global_capabilities
    for e in elevations:
        kind = e.get("kind")
        if kind in ("base_role", "scope_grant") and not can_assign(globales, role=e["role"]):
            return False
        if kind == "global_capability" and not can_assign(
            globales, global_capability=e["global_capability"]
        ):
            return False
        if kind == "capability_grant" and not can_assign(globales, capability=e["capability"]):
            return False
    if not elevations and not can_assign(globales, role="viewer"):
        return False
    return True


def not_assignable_error() -> AppHttpException:
    return AppHttpException(
        message=(
            "Tu función no permite asignar ese acceso: lo asigna una persona con "
            "'access_admin'."
        ),
        status_code=409,
        public_context={"code": CODE_NOT_ASSIGNABLE},
    )


def current_state(user_id: int, fila: dict | None = None) -> AccessState:
    """El acceso ACTUAL de ``user_id`` (rol base, globales, alcances), leído de la BD."""
    users = UserModel()
    ctx = users.find_access_context(user_id)
    if fila is None:
        fila = users.find_by_id(user_id) or {}
    return AccessState.of(
        fila.get("gateway_role") or ctx.get("role"), ctx.get("globals") or [], ctx.get("grants") or []
    )


def record_unapproved(
    *, admin, target_id: int, username: str, elevations: list[dict], origin: str, override=None,
    mode: str = MODE_UNAPPROVED,
) -> None:
    """
    La elevación se aplicó SIN segundo aprobador: ``ACCESS_FOUR_EYES=False``
    (``access.elevation_unapproved``) o la ventana de arranque (``mode=MODE_BOOTSTRAP``,
    ``access.bootstrap_assignment``). Fail-closed (``record_intent``) no: la escritura ya ocurrió.
    Se audita igual, con ``status=success``, y se loguea warning para que no pase en silencio.
    """
    bootstrap = mode == MODE_BOOTSTRAP
    if bootstrap:
        from app.services.bootstrap_window import ACTION_ASSIGNMENT

        action = ACTION_ASSIGNMENT
    else:
        action = ACTION_UNAPPROVED
    audit.record(
        action,
        admin=admin,
        target_type="user",
        target_id=target_id,
        touched_engine=False,
        grantee=username,
        detail=json.dumps(
            {
                "username": username,
                "origin": origin,
                "elevations": elevations,
                "sod_override": sod_service.override_payload(override),
                "four_eyes": bootstrap,
                "bootstrap_window": bootstrap,
            },
            ensure_ascii=False,
        ),
    )
    logger.warning(
        "Elevación de acceso SIN segundo aprobador (%s) sobre '%s': %s",
        "ventana de arranque" if bootstrap else "ACCESS_FOUR_EYES=False",
        username,
        ", ".join(e.get("kind", "?") for e in elevations) or "sod_override",
    )


class AccessRequestController:
    def __init__(self):
        self.requests = AccessChangeRequestModel()
        self.users = UserModel()

    # ------------------------------------------------------------------ #
    # Serialización                                                      #
    # ------------------------------------------------------------------ #
    def _usernames(self, ids: set) -> dict:
        from app.models.capability_grant_model import CapabilityGrantModel

        return CapabilityGrantModel().usernames({i for i in ids if i is not None})

    def _serialize_many(self, rows: list[dict]) -> list[dict]:
        ids = {r["target_user_id"] for r in rows}
        ids |= {r[k] for r in rows for k in ("requested_by", "decided_by")}
        names = self._usernames(ids)

        def ref(uid):
            return {"id": uid, "username": names.get(uid, "")} if uid is not None else None

        out = []
        for r in rows:
            d = r["desired"] or {}
            out.append(
                {
                    "id": r["id"],
                    "target": ref(r["target_user_id"]),
                    "requested_by": ref(r["requested_by"]),
                    "status": r["status"],
                    "origin": d.get("origin"),
                    "desired": {
                        "gateway_role": d.get("gateway_role"),
                        "global_capabilities": d.get("global_capabilities") or [],
                        "scope_grants": d.get("scope_grants") or [],
                    },
                    "elevations": d.get("elevations") or [],
                    "sod_override": d.get("sod_override"),
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "decided_by": ref(r["decided_by"]),
                    "decided_at": r["decided_at"],
                    "reason": r["reason"],
                }
            )
        return out

    def _serialize(self, row: dict) -> dict:
        return self._serialize_many([row])[0]

    # ------------------------------------------------------------------ #
    # Auditoría                                                          #
    # ------------------------------------------------------------------ #
    def _audit(self, action: str, actor, row: dict, *, status: str = "success",
               before: str | None, after: str | None, reason: str | None = None,
               extra: dict | None = None, system: bool = False) -> None:
        names = self._usernames({row["target_user_id"]})
        detail = {
            "request_id": row["id"],
            "before": {"status": before},
            "after": {"status": after},
            "requested_by": row.get("requested_by"),
            "elevations": (row.get("desired") or {}).get("elevations") or [],
        }
        if reason:
            detail["reason"] = reason
        if extra:
            detail.update(extra)
        audit.record(
            action,
            status=status,
            admin=None if system else actor,
            actor_type="system" if system else None,
            target_type="access_request",
            target_id=row["id"],
            touched_engine=False,
            grantee=names.get(row["target_user_id"]),
            detail=json.dumps(detail, ensure_ascii=False, default=str),
        )

    # ------------------------------------------------------------------ #
    # Alta (la llaman los escritores de gateway_user_controller)          #
    # ------------------------------------------------------------------ #
    def create(
        self,
        *,
        target_id: int,
        admin,
        desired: AccessState,
        elevations: list[dict],
        override,
        origin: str,
    ) -> dict:
        """
        Registra la parte que eleva como solicitud PENDIENTE y devuelve su forma pública.

        ``before_hash`` se toma del acceso ACTUAL (ya con la parte inmediata aplicada): es lo que
        el aprobador ve y lo que tiene que seguir igual al aprobar. Una pendiente previa sobre la
        misma persona se cancela (``superseded``).
        """
        actor_id, _ = identity_of(admin)
        for previa in self.requests.list_pending_for_target(target_id):
            if self.requests.close_pending(previa["id"], new_status="cancelled",
                                           decided_by=actor_id, reason="superseded"):
                self._audit(ACTION_CANCELLED, admin, previa, before="pending",
                            after="cancelled", reason="superseded")
        payload = {
            **desired.as_dict(),
            "sod_override": sod_service.override_payload(override),
            "elevations": elevations,
            "origin": origin,
        }
        row = self.requests.insert(
            target_user_id=target_id,
            requested_by=actor_id,
            desired=payload,
            before_hash=state_hash(current_state(target_id)),
        )
        self._audit(ACTION_CREATED, admin, row, before=None, after="pending",
                    extra={"desired": desired.as_dict(), "origin": origin,
                           "sod_override": payload["sod_override"]})
        return self._serialize(row)

    @staticmethod
    def pending_response(user: dict, request: dict) -> dict:
        """La forma de ``data`` en el ``202``: la persona como quedó YA, más la solicitud."""
        return {**user, "code": CODE_ELEVATION_PENDING, "pending_request": request}

    # ------------------------------------------------------------------ #
    # Vencimiento y cancelación automáticos                               #
    # ------------------------------------------------------------------ #
    def expire_overdue(self) -> int:
        n = 0
        for row in self.requests.list_overdue():
            if self.requests.close_pending(row["id"], new_status="expired", decided_by=None,
                                           reason="expired"):
                self._audit(ACTION_EXPIRED, None, row, before="pending", after="expired",
                            reason="expired", system=True)
                n += 1
        return n

    def cancel_pending_requested_by(self, user_id: int,
                                    *, reason: str = "requester_lost_access") -> int:
        """Quien pidió perdió ``access_admin`` o quedó inactivo: sus pendientes se cancelan."""
        n = 0
        for row in self.requests.list_pending_requested_by(user_id):
            if self.requests.close_pending(row["id"], new_status="cancelled", decided_by=None,
                                           reason=reason):
                self._audit(ACTION_CANCELLED, None, row, before="pending", after="cancelled",
                            reason=reason, system=True)
                n += 1
        return n

    # ------------------------------------------------------------------ #
    # Reglas                                                             #
    # ------------------------------------------------------------------ #
    def _requester_ok(self, requested_by: int | None) -> bool:
        if requested_by is None:
            return False
        fila = self.users.find_by_id(requested_by)
        if not fila or not fila.get("is_active"):
            return False
        ctx = self.users.find_access_context(requested_by)
        return GlobalCapability.ACCESS_ADMIN.value in (ctx.get("globals") or [])

    @staticmethod
    def _self_check(actor, row: dict) -> str | None:
        actor_id, _ = identity_of(actor)
        if row["requested_by"] is not None and row["requested_by"] == actor_id:
            return CODE_SELF_APPROVAL
        if row["target_user_id"] == actor_id:
            return CODE_SELF_MODIFICATION
        return None

    def _sod_plan(self, row: dict):
        """El veredicto de separación de deberes sobre el estado FINAL (lanza 409 / 422)."""
        from app.core.separation_of_duties import conflicts

        d = row["desired"] or {}
        desired = AccessState.from_dict(d)
        target = row["target_user_id"]
        found = conflicts(
            base_role=desired.base_role,
            scope_roles=list(desired.grants),
            globals_=list(desired.globals_),
            capabilities=sod_service.live_capability_keys(target),
        )
        return sod_service.check(
            found, covered=sod_service.covered_rules(target), override=d.get("sod_override")
        )

    def _block_reason(self, actor, row: dict, target: dict | None, requester_ok: bool) -> str | None:
        """
        Código ``access.*`` por el que ``actor`` NO puede aprobar ``row``, o ``None``. Única fuente
        de las reglas: ``approve`` y la bandeja la comparten.
        """
        code = self._self_check(actor, row)
        if code:
            return code
        if not target or not target.get("is_active"):
            return CODE_GRANT_USER_INACTIVE
        if not requester_ok:
            return CODE_REQUEST_NOT_PENDING
        if not actor_can_assign(actor, (row["desired"] or {}).get("elevations") or []):
            return CODE_NOT_ASSIGNABLE
        if state_hash(current_state(row["target_user_id"], target)) != row["before_hash"]:
            return CODE_REQUEST_STALE
        try:
            self._sod_plan(row)
        except AppHttpException as exc:
            return (exc.public_context or {}).get("code") or CODE_SOD_CONFLICT
        return None

    _BLOCK_MESSAGES = {
        CODE_SELF_APPROVAL: ("No puedes aprobar una elevación que pediste tú: la tiene que "
                             "aprobar otra persona con permiso de administración de accesos.", 409),
        CODE_SELF_MODIFICATION: ("No puedes aprobar una elevación de tu propio acceso.", 409),
        CODE_GRANT_USER_INACTIVE: ("La persona está desactivada: reactívala antes de aprobar.",
                                   409),
        CODE_NOT_ASSIGNABLE: ("Tu función no permite asignar ese acceso.", 409),
        CODE_REQUEST_STALE: ("El acceso de la persona cambió desde que se pidió la elevación: "
                             "la solicitud se canceló. Pedila de nuevo sobre el acceso actual.",
                             409),
    }

    @staticmethod
    def _not_pending() -> AppHttpException:
        return AppHttpException(
            message="La solicitud ya no está pendiente (se aplicó, se rechazó, venció o se canceló).",
            status_code=409,
            public_context={"code": CODE_REQUEST_NOT_PENDING},
        )

    def _pending_or_error(self, request_id: int) -> dict:
        row = self.requests.get(request_id)
        if not row:
            raise AppHttpException(
                message="Solicitud de acceso no encontrada.",
                status_code=404,
                public_context={"code": CODE_REQUEST_NOT_FOUND},
            )
        if row["status"] != "pending":
            raise self._not_pending()
        return row

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_pending(self, actor) -> list[dict]:
        """Bandeja: pendientes vigentes, cada una con ``can_decide`` / ``blocked_reason``."""
        self.expire_overdue()
        rows = self.requests.list_pending()
        out = self._serialize_many(rows)
        requesters: dict = {}
        for raw, ser in zip(rows, out):
            rid = raw["requested_by"]
            if rid not in requesters:
                requesters[rid] = self._requester_ok(rid)
            reason = self._block_reason(
                actor, raw, self.users.find_by_id(raw["target_user_id"]), requesters[rid]
            )
            ser["can_decide"] = reason is None
            ser["blocked_reason"] = reason
        return out

    def get(self, request_id: int) -> dict:
        row = self.requests.get(request_id)
        if not row:
            raise AppHttpException(
                message="Solicitud de acceso no encontrada.",
                status_code=404,
                public_context={"code": CODE_REQUEST_NOT_FOUND},
            )
        return self._serialize(row)

    # ------------------------------------------------------------------ #
    # Decisiones                                                         #
    # ------------------------------------------------------------------ #
    def approve(self, request_id: int, actor, reason: str | None = None) -> dict:
        from app.controllers.gateway_user_controller import GatewayUserController

        self.expire_overdue()
        row = self._pending_or_error(request_id)
        target_id = row["target_user_id"]
        target = self.users.find_by_id(target_id)
        username = (target or {}).get("username", "")

        requester_ok = self._requester_ok(row["requested_by"])
        if not requester_ok and self._self_check(actor, row) is None:
            # Deriva que se coló por los hooks: se cancela y se avisa.
            if self.requests.close_pending(request_id, new_status="cancelled", decided_by=None,
                                           reason="requester_lost_access"):
                self._audit(ACTION_CANCELLED, None, row, before="pending", after="cancelled",
                            reason="requester_lost_access", system=True)
            raise self._not_pending()

        code = self._block_reason(actor, row, target, requester_ok)
        if code is not None:
            self._audit(ACTION_APPROVED, actor, row, status="failure", before="pending",
                        after="pending", reason=code)
            if code == CODE_REQUEST_STALE:
                actor_id, _ = identity_of(actor)
                if self.requests.close_pending(request_id, new_status="cancelled",
                                               decided_by=actor_id, reason="stale"):
                    self._audit(ACTION_CANCELLED, actor, row, before="pending",
                                after="cancelled", reason="stale")
            if code not in self._BLOCK_MESSAGES:
                # Separación de deberes (409 con reglas y fuentes) o un override ya inválido
                # (422): se re-lanza el error rico del propio chequeo.
                self._sod_plan(row)
                raise self._not_pending()  # pragma: no cover — _sod_plan lanzó arriba
            message, status_code = self._BLOCK_MESSAGES[code]
            raise AppHttpException(message=message, status_code=status_code,
                                   public_context={"code": code})

        plan = self._sod_plan(row)
        desired = AccessState.from_dict(row["desired"] or {})
        actual = current_state(target_id, target)
        actor_id, _ = identity_of(actor)
        reason = (reason or "").strip() or None
        ctrl = GatewayUserController()
        antes = ctrl._access_snapshot(target)

        if plan:
            sod_service.record_override_intent(plan, admin=actor, target_id=target_id,
                                               username=username, approved_by=actor_id)

        quita_aa = (
            GlobalCapability.ACCESS_ADMIN.value in actual.globals_
            and GlobalCapability.ACCESS_ADMIN.value not in desired.globals_
        )
        accion_last_admin = "quitarle 'access_admin' a este usuario"

        def _claim(conn) -> None:
            if not self.requests.claim_in(conn, request_id, decided_by=actor_id, reason=reason):
                raise self._not_pending()

        self.users.replace_access(
            target_id,
            grants=sorted(desired.grants),
            globals_=sorted(desired.globals_),
            base_role=desired.base_role,
            last_admin_action=accion_last_admin if quita_aa else None,
            before_write=_claim,
        )
        if plan:
            sod_service.apply_override(plan, user_id=target_id, admin=actor, username=username,
                                       requested_by=row["requested_by"], approved_by=actor_id)
        else:
            sod_service.reconcile(target_id)

        despues = ctrl._access_snapshot(self.users.find_by_id(target_id))
        self._audit(ACTION_APPROVED, actor, row, before="pending", after="applied",
                    reason=reason)
        # El EFECTO, con la misma acción que el resto de los cambios de acceso: la pregunta "¿quién
        # le dio owner, y qué tenía antes?" se responde en un solo lugar.
        audit.record(
            "gateway_user.access_set",
            admin=actor,
            target_type="user",
            target_id=target_id,
            touched_engine=False,
            detail=ctrl._access_detail(
                username, antes, despues, request_id=request_id,
                requested_by=row["requested_by"], approved_by=actor_id,
            ),
        )
        from app.core import session_store

        session_store.revoke_all_for_user(target_id, session_store.REASON_ROLE_CHANGE)
        return self._serialize(self.requests.get(request_id))

    def reject(self, request_id: int, actor, reason: str | None = None) -> dict:
        """Pendiente → ``rejected``. Sin segundo aprobador: rechazar nunca da acceso."""
        self.expire_overdue()
        row = self._pending_or_error(request_id)
        actor_id, _ = identity_of(actor)
        reason = (reason or "").strip() or None
        if not self.requests.close_pending(request_id, new_status="rejected",
                                           decided_by=actor_id, reason=reason, only_live=True):
            raise self._not_pending()
        self._audit(ACTION_REJECTED, actor, row, before="pending", after="rejected", reason=reason)
        return self._serialize(self.requests.get(request_id))

    def cancel(self, request_id: int, actor, reason: str | None = None) -> dict:
        """Pendiente → ``cancelled``, y solo quien la pidió (los demás la rechazan)."""
        self.expire_overdue()
        row = self._pending_or_error(request_id)
        actor_id, _ = identity_of(actor)
        if row["requested_by"] is None or row["requested_by"] != actor_id:
            raise AppHttpException(
                message="Solo quien pidió la elevación puede cancelarla. Para descartarla, rechazala.",
                status_code=409,
                public_context={"code": CODE_REQUEST_NOT_REQUESTER},
            )
        reason = (reason or "").strip() or None
        if not self.requests.close_pending(request_id, new_status="cancelled",
                                           decided_by=actor_id, reason=reason, only_live=True):
            raise self._not_pending()
        self._audit(ACTION_CANCELLED, actor, row, before="pending", after="cancelled",
                    reason=reason or "cancelled_by_requester")
        return self._serialize(self.requests.get(request_id))
