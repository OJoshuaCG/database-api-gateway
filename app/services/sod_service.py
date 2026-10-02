"""
Separación de deberes: el ESCRITOR (409 / break-glass), la herencia y los reportes.

La regla pura vive en ``app/core/separation_of_duties.py``; el lector (neutralización al leer) en
``parse_access_context``. Este módulo junta lo que necesita la BD:

- ``check``: el veredicto de escritura sobre el estado RESULTANTE de una cuenta. Sin conflicto
  no cubierto, ``None``; con conflicto y sin ``sod_override``, 409 ``access.sod_conflict``; con
  override válido, un ``OverridePlan`` que el controller aplica después de escribir.
- ``record_override_intent`` / ``apply_override``: el break-glass. **En C2 se aplica en el acto**;
  C3 lo va a enrutar por el segundo aprobador (``approved_by``), igual que las elevaciones.
- ``grandfather_user``: la herencia de una combinación preexistente (``bootstrap_admin``).
- ``reconcile``: cierra las excepciones de reglas que la cuenta ya no viola.
- ``report_sod_violations`` / ``sod_report`` / ``warnings_for_user``: arranque, reporte y
  ``/auth/me``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.logger import get_logger
from app.core.separation_of_duties import conflicts, uncovered
from app.exceptions import AppHttpException
from app.services import audit
from app.services.capability_catalog import (
    CODE_SOD_CONFLICT,
    CODE_SOD_OVERRIDE_INVALID,
)

logger = get_logger(__name__)

#: Largo mínimo del motivo de un override: obliga a escribir una frase, no "ok".
OVERRIDE_REASON_MIN_LENGTH = 20
OVERRIDE_REASON_MAX_LENGTH = 500
#: Vigencia máxima (y por defecto) de un override, en horas: 7 días.
OVERRIDE_MAX_HOURS = 7 * 24

ACTION_OVERRIDE = "access.sod_override"
ACTION_GRANDFATHERED = "access.sod_grandfathered"


@dataclass(frozen=True)
class OverridePlan:
    """Un break-glass validado, pendiente de aplicar DESPUÉS de la escritura del acceso."""

    conflicts: dict
    reason: str
    expires_at: datetime
    rules: tuple[str, ...] = field(default=())


def _utcnow() -> datetime:
    from app.models.sod_exception_model import utcnow

    return utcnow()


# --------------------------------------------------------------------------- #
# Estado resultante                                                            #
# --------------------------------------------------------------------------- #


def live_capability_keys(user_id: int) -> list[tuple[str, str, int]]:
    """
    Capacidades puntuales VIVAS (pendientes y activas): las que cuentan al ESCRIBIR, porque una
    pendiente surte efecto en cuanto se aprueba.
    """
    from app.models.capability_grant_model import CapabilityGrantModel

    return [
        (f["capability"], f["scope_type"], int(f["scope_id"]))
        for f in CapabilityGrantModel().list_live_for_user(user_id)
    ]


def current_conflicts(user_id: int) -> dict[str, list[dict]]:
    """Las reglas que la cuenta viola HOY (con sus capacidades puntuales vivas)."""
    from app.models.user_model import UserModel

    ctx = UserModel().find_access_context(user_id)
    return conflicts(
        base_role=ctx.get("role"),
        scope_roles=ctx.get("grants") or [],
        globals_=ctx.get("globals") or [],
        capabilities=live_capability_keys(user_id),
    )


def covered_rules(user_id: int | None) -> list[str]:
    if user_id is None:
        return []
    from app.models.sod_exception_model import SodExceptionModel

    return SodExceptionModel().live_rules(user_id)


# --------------------------------------------------------------------------- #
# Escritor                                                                     #
# --------------------------------------------------------------------------- #


def conflict_error(found: dict[str, list[dict]]) -> AppHttpException:
    """El 409 ``access.sod_conflict``: nombra las reglas y las fuentes que chocan."""
    reglas = sorted(found)
    return AppHttpException(
        message=(
            "Esta combinación de acceso viola la separación de deberes: 'security_officer' no "
            "puede convivir con 'owner' ni con 'access_admin' en la misma cuenta. Repartí las "
            "funciones en cuentas distintas o declará un 'sod_override' con motivo."
        ),
        status_code=409,
        public_context={
            "code": CODE_SOD_CONFLICT,
            "rules": reglas,
            "conflicts": [{"rule": r, "sources": found[r]} for r in reglas],
            "override": {
                "field": "sod_override",
                "reason_min_length": OVERRIDE_REASON_MIN_LENGTH,
                "max_hours": OVERRIDE_MAX_HOURS,
            },
        },
    )


def _override_invalid(message: str) -> AppHttpException:
    return AppHttpException(
        message=message,
        status_code=422,
        public_context={
            "code": CODE_SOD_OVERRIDE_INVALID,
            "reason_min_length": OVERRIDE_REASON_MIN_LENGTH,
            "max_hours": OVERRIDE_MAX_HOURS,
        },
    )


def _validate_override(override) -> tuple[str, int]:
    if not isinstance(override, dict):
        raise _override_invalid("'sod_override' tiene que ser un objeto con 'reason'.")
    reason = (override.get("reason") or "").strip()
    if len(reason) < OVERRIDE_REASON_MIN_LENGTH:
        raise _override_invalid(
            f"El motivo del override tiene que tener al menos {OVERRIDE_REASON_MIN_LENGTH} "
            "caracteres."
        )
    if len(reason) > OVERRIDE_REASON_MAX_LENGTH:
        raise _override_invalid(
            f"El motivo del override no puede superar {OVERRIDE_REASON_MAX_LENGTH} caracteres."
        )
    horas = override.get("expires_in_hours")
    if horas is None:
        horas = OVERRIDE_MAX_HOURS
    if isinstance(horas, bool) or not isinstance(horas, int) or not 1 <= horas <= OVERRIDE_MAX_HOURS:
        raise _override_invalid(
            f"La vigencia del override va de 1 a {OVERRIDE_MAX_HOURS} horas (7 días)."
        )
    return reason, horas


def check(
    found: dict[str, list[dict]], *, covered, override
) -> OverridePlan | None:
    """
    Veredicto de escritura sobre el estado RESULTANTE (``found = conflicts(...)``).

    - Ninguna regla violada sin cubrir → ``None`` (incluye la cuenta heredada a la que se le edita
      algo que no agrega una regla nueva: su excepción viva la cubre).
    - Alguna sin cubrir y sin ``override`` → 409 ``access.sod_conflict``.
    - Con ``override`` → se valida (422 ``access.sod_override_invalid``) y se devuelve el plan.
      Un ``override`` sin conflicto se ignora: no se escribe una excepción que no exceptúa nada.
    """
    sin_cubrir = uncovered(found, covered)
    if not sin_cubrir:
        return None
    if not override:
        raise conflict_error(sin_cubrir)
    reason, horas = _validate_override(override)
    return OverridePlan(
        conflicts=sin_cubrir,
        reason=reason,
        expires_at=_utcnow() + timedelta(hours=horas),
        rules=tuple(sorted(sin_cubrir)),
    )


def _override_detail(plan: OverridePlan, username: str, **extra) -> str:
    return json.dumps(
        {
            "username": username,
            "rules": list(plan.rules),
            "conflicts": [{"rule": r, "sources": plan.conflicts[r]} for r in plan.rules],
            "reason": plan.reason,
            "expires_at": plan.expires_at.isoformat(),
            # C2: el override se aplica sin segundo aprobador. C3 lo va a dejar pendiente.
            "approved_by": None,
            **extra,
        },
        ensure_ascii=False,
    )


def record_override_intent(plan: OverridePlan, *, admin, target_id: int | None, username: str) -> None:
    """
    Rastro OBLIGATORIO del break-glass, ANTES de escribir. Fail-closed (``record_intent``): si no
    se puede auditar, la escritura no ocurre — un override sin rastro es justo lo que la regla
    existe para impedir.
    """
    audit.record_intent(
        ACTION_OVERRIDE,
        admin=admin,
        target_type="user",
        target_id=target_id,
        touched_engine=False,
        detail=_override_detail(plan, username),
    )


def apply_override(plan: OverridePlan, *, user_id: int, admin, username: str) -> list[dict]:
    """
    Escribe una fila de ``sod_exceptions`` por regla y audita el éxito.

    Va DESPUÉS de la escritura del acceso a propósito: si la escritura de la excepción fallara,
    el lector descarta ``security_officer`` (falla cerrado). Al revés, un fallo del acceso dejaría
    una excepción viva cubriendo una combinación que nadie aplicó todavía.

    TODO(C3): enrutar el override por el segundo aprobador: la fila nace sin cubrir nada hasta
    que otro ``access_admin`` la apruebe (``approved_by``).
    """
    from app.core.actor import identity_of
    from app.models.sod_exception_model import SodExceptionModel

    actor_id, _ = identity_of(admin)
    modelo = SodExceptionModel()
    filas = [
        modelo.insert(
            user_id=user_id,
            rule=rule,
            reason=plan.reason,
            requested_by=actor_id,
            expires_at=plan.expires_at,
        )
        for rule in plan.rules
    ]
    audit.record(
        ACTION_OVERRIDE,
        admin=admin,
        target_type="user",
        target_id=user_id,
        touched_engine=False,
        detail=_override_detail(plan, username, exception_ids=[f["id"] for f in filas]),
    )
    logger.warning(
        "Override de separación de deberes aplicado sobre '%s' (%s), vence %s",
        username,
        ",".join(plan.rules),
        plan.expires_at.isoformat(),
    )
    return filas


def reconcile(user_id: int) -> int:
    """
    Cierra las excepciones vivas de reglas que la cuenta YA NO viola. Best-effort.

    Sin esto, una excepción (sobre todo la heredada, que no vence) sería una licencia permanente:
    quitarle ``security_officer`` a la cuenta y devolvérselo un mes después pasaría sin 409.
    """
    try:
        from app.models.sod_exception_model import SodExceptionModel

        vivas = SodExceptionModel().live_rules(user_id)
        if not vivas:
            return 0
        violadas = current_conflicts(user_id)
        resueltas = [r for r in vivas if r not in violadas]
        return SodExceptionModel().close_live(user_id, resueltas)
    except Exception:  # noqa: BLE001 — el acceso ya se escribió; esto es prolijidad
        logger.exception("No se pudieron cerrar las excepciones resueltas de %s", user_id)
        return 0


# --------------------------------------------------------------------------- #
# Herencia (bootstrap)                                                         #
# --------------------------------------------------------------------------- #


def grandfather_user(user_id: int) -> list[dict]:
    """
    Hereda la combinación ACTUAL de ``user_id``: una fila ``grandfathered`` por regla violada
    sin excepción viva. Idempotente.

    La llama ``bootstrap_admin`` al sembrar o revivir la cuenta combinada (``owner`` +
    ``access_admin`` + ``security_officer``): sin esto, la instalación nueva —y cada test— nacería
    con el administrador neutralizado al leer. C4 cambia la siembra y esto deja de hacer falta.
    """
    from app.models.sod_exception_model import SodExceptionModel

    violadas = current_conflicts(user_id)
    if not violadas:
        return []
    return SodExceptionModel().grandfather(user_id, sorted(violadas))


# --------------------------------------------------------------------------- #
# Reportes                                                                     #
# --------------------------------------------------------------------------- #

#: Filas heredadas ya reportadas en ESTE arranque (``(id, user_id, rule)``). El proceso es un
#: arranque, así que "una vez por fila por arranque" es un set en memoria.
_reported: set[tuple[int, int, str]] = set()


def reset_sod_report_state() -> None:
    """Olvida lo reportado. Para los tests, donde un proceso hace muchos arranques."""
    _reported.clear()


def report_sod_violations() -> int:
    """
    Arranque: por cada excepción HEREDADA viva, un warning y una fila ``access.sod_grandfathered``
    (``actor_type='system'``), una sola vez por arranque. Una heredada cuya cuenta ya no viola la
    regla se cierra (``resolved``) en vez de reportarse. Devuelve cuántas reportó.

    No lanza: el llamador (``lifespan``) lo envuelve igual, porque un reporte no puede impedir el
    arranque.
    """
    from app.models.capability_grant_model import CapabilityGrantModel
    from app.models.sod_exception_model import SodExceptionModel

    modelo = SodExceptionModel()
    filas = [f for f in modelo.list_live() if f["grandfathered"]]
    if not filas:
        return 0
    nombres = CapabilityGrantModel().usernames({f["user_id"] for f in filas})
    violadas_por_usuario: dict[int, dict] = {}
    n = 0
    for f in filas:
        uid = f["user_id"]
        if uid not in violadas_por_usuario:
            violadas_por_usuario[uid] = current_conflicts(uid)
        if f["rule"] not in violadas_por_usuario[uid]:
            modelo.close_live(uid, [f["rule"]])
            continue
        clave = (f["id"], uid, f["rule"])
        if clave in _reported:
            continue
        _reported.add(clave)
        username = nombres.get(uid)
        logger.warning(
            "Separación de deberes: '%s' mantiene la combinación heredada %s (desde %s). "
            "Repartí las funciones en cuentas distintas.",
            username,
            f["rule"],
            f["created_at"],
        )
        audit.record(
            ACTION_GRANDFATHERED,
            admin=None,
            actor_type="system",
            target_type="user",
            target_id=uid,
            touched_engine=False,
            detail=json.dumps(
                {
                    "username": username,
                    "rule": f["rule"],
                    "exception_id": f["id"],
                    "since": f["created_at"].isoformat() if f["created_at"] else None,
                    "sources": violadas_por_usuario[uid][f["rule"]],
                },
                ensure_ascii=False,
            ),
        )
        n += 1
    return n


def _ref(uid: int | None, nombres: dict) -> dict | None:
    return {"id": uid, "username": nombres.get(uid, "")} if uid is not None else None


def sod_report() -> dict:
    """
    ``GET /authz/sod-report``: las excepciones vivas (heredadas y overrides) y las cuentas que
    violan una regla SIN excepción (al leer se les descarta ``security_officer``).
    """
    from app.models.capability_grant_model import CapabilityGrantModel
    from app.models.sod_exception_model import SodExceptionModel

    modelo = SodExceptionModel()
    filas = modelo.list_live()
    ids = {f["user_id"] for f in filas}
    ids |= {f[k] for f in filas for k in ("requested_by", "approved_by") if f[k] is not None}
    oficiales = modelo.users_with_security_officer()
    ids |= {u["id"] for u in oficiales}
    nombres = CapabilityGrantModel().usernames(ids)

    violadas_por_usuario = {u["id"]: current_conflicts(u["id"]) for u in oficiales}
    activos = {u["id"]: u["is_active"] for u in oficiales}
    excepciones = [
        {
            "id": f["id"],
            "user": _ref(f["user_id"], nombres),
            "user_active": activos.get(f["user_id"]),
            "rule": f["rule"],
            "kind": "grandfathered" if f["grandfathered"] else "override",
            "reason": f["reason"],
            "since": f["created_at"],
            "expires_at": f["expires_at"],
            "requested_by": _ref(f["requested_by"], nombres),
            "approved_by": _ref(f["approved_by"], nombres),
            "still_violating": f["rule"] in violadas_por_usuario.get(f["user_id"], {}),
        }
        for f in filas
    ]
    cubiertas: dict[int, set[str]] = {}
    for f in filas:
        cubiertas.setdefault(f["user_id"], set()).add(f["rule"])
    sin_cubrir = []
    for u in oficiales:
        resto = uncovered(violadas_por_usuario[u["id"]], cubiertas.get(u["id"], ()))
        if resto:
            sin_cubrir.append(
                {
                    "user": _ref(u["id"], nombres),
                    "user_active": u["is_active"],
                    "rules": sorted(resto),
                }
            )
    return {"exceptions": excepciones, "uncovered": sin_cubrir}


def warnings_for_user(user_id: int) -> list[dict]:
    """
    ``/auth/me.sod_warnings`` de la persona de la sesión: cada regla que su cuenta viola, con la
    excepción que la cubre (``grandfathered`` | ``override``) o ``neutralized`` si ninguna la
    cubre (en cuyo caso el lector ya le descartó ``security_officer``). Vacío si no viola nada.

    Mismo estado que el LECTOR (capacidades puntuales activas), así que lo que se avisa es lo que
    se aplica. Nunca lanza: ``/auth/me`` es lo que arranca la SPA.
    """
    try:
        from app.core.capability_resolution import parse_access_context
        from app.models.sod_exception_model import SodExceptionModel
        from app.models.user_model import UserModel

        ctx = UserModel().find_access_context(user_id)
        if "security_officer" not in (ctx.get("globals") or []):
            return []
        # Las globales salen CRUDAS del ctx: las de `parsed` ya vienen sin `security_officer`
        # si el lector lo neutralizó, y justo eso es lo que hay que avisar.
        parsed = parse_access_context(ctx)
        found = conflicts(
            base_role=parsed.base,
            scope_roles=parsed.scope_roles,
            globals_=ctx.get("globals") or [],
            capabilities=[(c, t, i) for c, t, i, _ in parsed.capability_grants],
        )
        if not found:
            return []
        vivas: dict[str, dict] = {}
        for f in SodExceptionModel().list_live(user_id):
            vivas.setdefault(f["rule"], f)
        out = []
        for rule in sorted(found):
            exc = vivas.get(rule)
            out.append(
                {
                    "rule": rule,
                    "status": (
                        "neutralized"
                        if exc is None
                        else ("grandfathered" if exc["grandfathered"] else "override")
                    ),
                    "reason": exc["reason"] if exc else None,
                    "since": exc["created_at"] if exc else None,
                    "expires_at": exc["expires_at"] if exc else None,
                }
            )
        return out
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron calcular los avisos de separación de deberes.")
        return []
