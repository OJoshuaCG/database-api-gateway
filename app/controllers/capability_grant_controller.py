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
- Política de ASIGNACIÓN (C3, ``ASSIGNABLE_BY``): ``access_admin`` asigna cualquier capacidad
  otorgable. Reemplaza al techo por TENENCIA ("quien otorga tiene que tener la capacidad en ese
  alcance"), que obligaba a quien administra accesos a tener cada deber que reparte.
- Las 11 capacidades sensibles (``is_sensitive``: las exclusivas de ``owner``) nacen ``pending`` y
  no surten efecto hasta que un SEGUNDO access_admin las apruebe. El resto nace ``active``. Con
  ``ACCESS_FOUR_EYES=False`` nacen ``active`` y se audita ``access.elevation_unapproved``; dentro
  de la ventana de arranque (C4) también, auditadas ``access.bootstrap_assignment``.
- Revocar lo hace un solo access_admin, sin techo: activa → ``revoked``, pendiente → ``cancelled``.
- Separación de deberes: a una persona con ``security_officer`` no se le otorga (ni se le aprueba)
  una capacidad exclusiva de ``owner`` sin excepción viva o ``sod_override`` (409
  ``access.sod_conflict``). Ver ``app/core/separation_of_duties.py``. El ``sod_override`` es una
  elevación: la capacidad nace ``pending`` con el override guardado (``sod_override_json``) y la
  excepción se escribe al APROBARLA, con ``approved_by``.

Aprobación (B4)
---------------
- Aprueba OTRO access_admin: ni quien la pidió (``access.self_approval_forbidden``) ni la propia
  persona destino (``access.self_modification_forbidden``).
- Quien APRUEBA tiene que poder asignarla (``ASSIGNABLE_BY``; ya no hace falta que la tenga); del
  solicitante solo se exige que siga siendo un access_admin activo. Si dejó de serlo, la solicitud
  se cancela y se responde 409.
- La decisión es compare-and-set (``status='pending' AND expires_at > now``): con dos aprobadores
  simultáneos gana uno solo y el otro recibe ``access.grant_not_pending``.
- Vencimiento perezoso (D6): ``expire_overdue`` corre en cada lectura/decisión y una vez al
  arrancar. Los eventos automáticos (vencida, cancelada por pérdida de rol) se auditan con
  ``actor_type="system"``.

Auditoría (D11): ``audit.record`` con ``privilege=capacidad``, ``grantee=username``,
``object_level=scope_type``, ``object_name="environment:3"`` y un ``detail`` JSON con el diff de
estado. Los rechazos por auto-otorgamiento y asignación se auditan como ``failure``.
"""

import json
import uuid

from app.controllers import access_request_controller as access_requests
from app.core.actor import identity_of
from app.controllers.gateway_user_controller import CODE_NOT_FOUND
from app.exceptions import AppHttpException
from app.models.capability_grant_model import CapabilityGrantModel
from app.models.user_model import UserModel
from app.services import audit, sod_service
from app.services.capability_catalog import (
    CODE_CAPABILITY_NOT_GRANTABLE,
    CODE_GRANT_BULK_FAILED,
    CODE_GRANT_DUPLICATE,
    CODE_GRANT_NOT_FOUND,
    CODE_GRANT_NOT_PENDING,
    CODE_GRANT_SCOPE_NOT_FOUND,
    CODE_GRANT_USER_INACTIVE,
    CODE_NOT_ASSIGNABLE,
    CODE_SELF_APPROVAL,
    CODE_SELF_MODIFICATION,
    CODE_SOD_CONFLICT,
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
        bulk_id: str | None = None,
    ) -> None:
        detail = {"before": {"status": before}, "after": {"status": after}, "grant_id": grant_id}
        if reason:
            detail["reason"] = reason
        if bulk_id:
            # Correlación de un alta masiva: vive en el JSON del detalle, sin columna ni migración.
            detail["bulk_id"] = bulk_id
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
        (404) y activo (409) → otorgable (422) → alcance existe (404) → asignable (409) → duplicado
        (409, con el ``UNIQUE`` de respaldo) → separación de deberes (409 / 422 del override).

        Nace ``pending`` si es sensible o trae un ``sod_override`` (las dos son elevaciones), salvo
        ``ACCESS_FOUR_EYES=False``.
        """
        capability = data["capability"]
        scope_type, scope_id = data["scope_type"], int(data["scope_id"])
        sensitive = is_sensitive(capability)
        action = (
            "capability_grant.requested"
            if sensitive and access_requests.four_eyes()
            else "capability_grant.created"
        )

        user = self._user_or_404(user_id)
        username = user["username"]

        self._precheck_request(user, user_id, actor, capability, scope_type, scope_id, action)

        if self.grants.scope_name(scope_type, scope_id) is None:
            raise self._scope_not_found()

        self._check_assignable(actor, username, capability, scope_type, scope_id, action)

        if self.grants.find_live(user_id, capability, scope_type, scope_id):
            raise self._duplicate()

        plan = self._sod_plan_for_target(
            user_id, username, actor, capability, scope_type, scope_id,
            data.get("sod_override"), action,
        )
        elevaciones = (
            [{"kind": "capability_grant", "capability": capability,
              "scope_type": scope_type, "scope_id": scope_id}]
            if sensitive else []
        )
        modo = access_requests.decide(elevaciones, plan, actor)
        pending = modo == access_requests.MODE_WAIT
        # La acción se fija con la salida decidida: una sensible que la ventana de arranque aplica
        # en el acto nace `active` y se audita como creada, no como pedida.
        action = "capability_grant.requested" if pending else "capability_grant.created"
        if plan and not pending:
            sod_service.record_override_intent(plan, admin=actor, target_id=user_id,
                                               username=username)

        actor_id, _ = identity_of(actor)
        row = self.grants.insert(
            user_id=user_id,
            capability=capability,
            scope_type=scope_type,
            scope_id=scope_id,
            requested_by=actor_id,
            pending=pending,
            reason=(data.get("reason") or "").strip() or None,
            sod_override=sod_service.override_payload(data.get("sod_override")) if plan else None,
        )
        if plan and not pending:
            sod_service.apply_override(plan, user_id=user_id, admin=actor, username=username)
        self._audit(action, actor, username, capability, scope_type, scope_id,
                    grant_id=row["id"], before=None, after=row["status"],
                    reason=row.get("request_reason"))
        if modo and not pending:
            access_requests.record_unapproved(
                admin=actor, target_id=user_id, username=username, elevations=elevaciones,
                origin="capability_grant", override=data.get("sod_override") if plan else None,
                mode=modo,
            )
        return self._serialize(row)

    # -- Validaciones compartidas por ``create`` y ``create_bulk`` ----------------------------- #
    def _precheck_request(self, user: dict, user_id: int, actor, capability: str,
                          scope_type: str, audit_scope_id: int, action: str) -> None:
        """Lo que no depende del destino: auto-otorgamiento (409), persona activa (409), otorgable (422)."""
        if self._is_self(actor, user_id):
            self._audit(action, actor, user["username"], capability, scope_type, audit_scope_id,
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

    def _check_assignable(self, actor, username: str, capability: str, scope_type: str,
                          audit_scope_id: int, action: str) -> None:
        if not self._assignable(actor, capability):
            self._audit(action, actor, username, capability, scope_type, audit_scope_id,
                        grant_id=None, before=None, after=None, status="failure",
                        reason="not_assignable")
            raise access_requests.not_assignable_error()

    @staticmethod
    def _scope_not_found() -> AppHttpException:
        return AppHttpException(
            message="El entorno o servidor indicado no existe.",
            status_code=404,
            public_context={"code": CODE_GRANT_SCOPE_NOT_FOUND},
        )

    @staticmethod
    def _duplicate() -> AppHttpException:
        return AppHttpException(
            message="Ya existe una capacidad puntual viva igual para esta persona.",
            status_code=409,
            public_context={"code": CODE_GRANT_DUPLICATE},
        )

    def _sod_plan_for_target(self, user_id: int, username: str, actor, capability: str,
                             scope_type: str, scope_id: int, override, action: str):
        """
        Separación de deberes sobre el estado RESULTANTE: lo que la persona tiene más esta
        capacidad (aunque nazca pendiente: surte efecto en cuanto se aprueba). 409 si conflicta.
        """
        found = self._sod_conflicts(user_id, extra=(capability, scope_type, scope_id))
        try:
            return sod_service.check(
                found, covered=sod_service.covered_rules(user_id), override=override
            )
        except AppHttpException as exc:
            if (exc.public_context or {}).get("code") == CODE_SOD_CONFLICT:
                self._audit(action, actor, username, capability, scope_type, scope_id,
                            grant_id=None, before=None, after=None, status="failure",
                            reason="sod_conflict")
            raise

    def create_bulk(self, user_id: int, data: dict, actor) -> dict:
        """
        La misma capacidad sobre VARIOS destinos (``scope_ids``), todo o nada.

        Se valida TODO antes de insertar: los chequeos de la persona y la capacidad (como
        ``create``, el primero que falla corta) y, por destino, alcance existe / duplicado /
        separación de deberes. Si algún destino falla, 409 ``access.grant_bulk_failed`` con
        ``failures=[{scope_id, code, message}]`` (TODOS los que fallan) y no se inserta nada.

        ``access_requests.decide`` corre UNA vez: todas las filas nacen con el mismo modo. Las
        filas entran en una sola transacción (``insert_many``); la auditoría es una por fila, con
        un ``bulk_id`` común en el ``detail``, y la elevación sin segundo aprobador se audita una
        vez por pedido (``record_unapproved``).
        """
        capability = data["capability"]
        scope_type = data["scope_type"]
        scope_ids = list(dict.fromkeys(int(i) for i in data["scope_ids"]))
        override = data.get("sod_override")
        sensitive = is_sensitive(capability)
        action = (
            "capability_grant.requested"
            if sensitive and access_requests.four_eyes()
            else "capability_grant.created"
        )

        user = self._user_or_404(user_id)
        username = user["username"]
        first = scope_ids[0]

        self._precheck_request(user, user_id, actor, capability, scope_type, first, action)
        self._check_assignable(actor, username, capability, scope_type, first, action)

        known = self.grants.scope_names([(scope_type, i) for i in scope_ids])
        live = self.grants.live_scope_ids(user_id, capability, scope_type, scope_ids)
        failures: list[dict] = []
        plans: dict[int, object] = {}

        def fail(scope_id: int, exc: AppHttpException) -> None:
            public = dict(exc.public_context or {})
            entry = {"scope_id": scope_id, "code": public.pop("code", None), "message": exc.message}
            if public:
                # ``access.sod_conflict`` trae ``conflicts`` y los límites del ``override``: la SPA
                # los necesita para ofrecer la excepción de emergencia sobre todo el lote.
                entry["context"] = public
            failures.append(entry)

        for scope_id in scope_ids:
            if (scope_type, scope_id) not in known:
                fail(scope_id, self._scope_not_found())
            elif scope_id in live:
                fail(scope_id, self._duplicate())
            else:
                try:
                    plans[scope_id] = self._sod_plan_for_target(
                        user_id, username, actor, capability, scope_type, scope_id,
                        override, action,
                    )
                except AppHttpException as exc:
                    fail(scope_id, exc)

        if failures:
            raise AppHttpException(
                message="No se otorgó nada: hay destinos que no se pueden otorgar.",
                status_code=409,
                public_context={"code": CODE_GRANT_BULK_FAILED, "failures": failures},
            )

        elevaciones = (
            [{"kind": "capability_grant", "capability": capability,
              "scope_type": scope_type, "scope_id": i} for i in scope_ids]
            if sensitive else []
        )
        # Una sola decisión para todo el lote (ver ``decide``): mismo modo en todas las filas.
        distinct_plans = list({p.rules: p for p in plans.values() if p}.values())
        modo = access_requests.decide(elevaciones, distinct_plans[0] if distinct_plans else None,
                                      actor)
        pending = modo == access_requests.MODE_WAIT
        action = "capability_grant.requested" if pending else "capability_grant.created"
        if distinct_plans and not pending:
            for plan in distinct_plans:
                sod_service.record_override_intent(plan, admin=actor, target_id=user_id,
                                                   username=username)

        actor_id, _ = identity_of(actor)
        rows = self.grants.insert_many(
            user_id=user_id,
            capability=capability,
            scope_type=scope_type,
            scope_ids=scope_ids,
            requested_by=actor_id,
            pending=pending,
            reason=(data.get("reason") or "").strip() or None,
            sod_override=sod_service.override_payload(override) if distinct_plans else None,
        )
        if distinct_plans and not pending:
            for plan in distinct_plans:
                sod_service.apply_override(plan, user_id=user_id, admin=actor, username=username)
        bulk_id = uuid.uuid4().hex
        for row in rows:
            self._audit(action, actor, username, capability, scope_type, row["scope_id"],
                        grant_id=row["id"], before=None, after=row["status"],
                        reason=row.get("request_reason"), bulk_id=bulk_id)
        if modo and not pending:
            access_requests.record_unapproved(
                admin=actor, target_id=user_id, username=username, elevations=elevaciones,
                origin="capability_grant", override=override if distinct_plans else None,
                mode=modo,
            )
        return {
            "count": len(rows),
            "pending": pending,
            "grants": self._serialize_many(rows),
        }

    @staticmethod
    def _assignable(actor, capability: str) -> bool:
        """¿La función del actor asigna esta capacidad? (``ASSIGNABLE_BY``; fail-closed)."""
        return access_requests.actor_can_assign(
            actor, [{"kind": "capability_grant", "capability": capability}]
        )

    def _sod_conflicts(self, user_id: int, *, extra: tuple | None = None) -> dict:
        """
        Reglas de separación de deberes que viola el estado de ``user_id`` con sus capacidades
        puntuales VIVAS (más ``extra``, la que se está por otorgar). Ver
        ``app/core/separation_of_duties.py``.
        """
        from app.core.separation_of_duties import conflicts

        ctx = self.users.find_access_context(user_id)
        capacidades = sod_service.live_capability_keys(user_id)
        if extra is not None:
            capacidades.append(extra)
        return conflicts(
            base_role=ctx.get("role"),
            scope_roles=ctx.get("grants") or [],
            globals_=ctx.get("globals") or [],
            capabilities=capacidades,
        )

    def _sod_uncovered(self, user_id: int) -> dict:
        """Lo que ``_sod_conflicts`` encuentra y ninguna excepción viva cubre."""
        from app.core.separation_of_duties import uncovered

        return uncovered(self._sod_conflicts(user_id), sod_service.covered_rules(user_id))

    def _sod_plan(self, row: dict):
        """
        Veredicto de separación de deberes al APROBAR, con el ``sod_override`` que viajó con la
        solicitud: ``None`` si no hay conflicto sin cubrir, el plan si el override lo cubre; lanza
        409 ``access.sod_conflict`` si no hay override (o 422 si quedó inválido).
        """
        return sod_service.check(
            self._sod_conflicts(row["user_id"]),
            covered=sod_service.covered_rules(row["user_id"]),
            override=row.get("sod_override"),
        )

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
        sod_service.reconcile(user_id)
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
        if not is_grantable(row["capability"]):
            return CODE_CAPABILITY_NOT_GRANTABLE
        if not self._assignable(actor, row["capability"]):
            return CODE_NOT_ASSIGNABLE
        # Re-chequeo de la separación de deberes al aprobar: entre el alta y la aprobación la
        # persona pudo recibir `security_officer`, o vencer el override que cubría la regla. La
        # solicitud pendiente ya cuenta como viva en el estado; su override, si trajo, cubre.
        try:
            self._sod_plan(row)
        except AppHttpException:
            return CODE_SOD_CONFLICT
        return None

    _BLOCK_MESSAGES = {
        CODE_SELF_APPROVAL: ("No puedes aprobar una solicitud que pediste tú: la tiene que "
                             "aprobar otra persona con permiso de administración de accesos.", 409),
        CODE_SELF_MODIFICATION: ("No puedes aprobarte capacidades a ti mismo.", 409),
        CODE_GRANT_USER_INACTIVE: ("La persona está desactivada: reactívala antes de aprobar.", 409),
        CODE_GRANT_SCOPE_NOT_FOUND: ("El entorno o servidor indicado ya no existe.", 404),
        CODE_NOT_ASSIGNABLE: ("Tu función no permite asignar esa capacidad.", 409),
        CODE_CAPABILITY_NOT_GRANTABLE: ("Esa capacidad no se puede otorgar de forma puntual.", 422),
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
            if code == CODE_SOD_CONFLICT:
                self._sod_plan(row)  # re-lanza el 409 (o el 422) con reglas y fuentes
                raise sod_service.conflict_error(self._sod_uncovered(row["user_id"]))
            message, status_code = self._BLOCK_MESSAGES.get(
                code, ("No se puede aprobar esta solicitud.", 409)
            )
            raise AppHttpException(message=message, status_code=status_code,
                                   public_context={"code": code})

        actor_id, _ = identity_of(actor)
        reason = (reason or "").strip() or None
        plan = self._sod_plan(row)
        if plan:
            sod_service.record_override_intent(plan, admin=actor, target_id=row["user_id"],
                                               username=username, approved_by=actor_id)
        if not self.grants.decide_pending(grant_id, approve=True, decided_by=actor_id,
                                          reason=reason):
            raise self._not_pending()
        if plan:
            sod_service.apply_override(plan, user_id=row["user_id"], admin=actor,
                                       username=username, requested_by=row["requested_by"],
                                       approved_by=actor_id)
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
        sod_service.reconcile(row["user_id"])
        return self._serialize(self.grants.get(grant_id))
