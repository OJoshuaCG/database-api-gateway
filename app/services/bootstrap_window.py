"""
La VENTANA DE ARRANQUE de los accesos (C4): cómo una instalación con UN solo ``access_admin``
completa su primera configuración sin apagar los cuatro ojos.

EL PROBLEMA
-----------
Desde C3 toda elevación espera a un segundo ``access_admin`` (``access_request_controller``). Una
instalación nueva siembra uno solo, y crear el segundo es en sí una elevación: sin una salida, el
primer arranque no podría crear ni el ``security_officer``, ni el ``owner``, ni a quien aprueba.
Apagar ``ACCESS_FOUR_EYES`` resuelve eso a costa de apagar la barrera para siempre.

LA SALIDA, ACOTADA
------------------
Mientras la ventana está ABIERTA y hay un solo ``access_admin`` activo con credencial —el que
pide—, sus elevaciones se aplican en el acto y cada una se audita ``access.bootstrap_assignment``
(``applies_to``). La ventana se cierra **para siempre** con lo primero que pase:

- un segundo ``access_admin`` activo ACEPTÓ su invitación (``second_admin``): desde ahí hay quien
  apruebe, así que la excepción deja de hacer falta;
- vence ``ACCESS_BOOTSTRAP_WINDOW_HOURS`` (``deadline``, 72 h por defecto) desde que se abrió.

"Con credencial" es el mismo criterio que el invariante del último administrador
(``count_active_access_admins``): un ``access_admin`` con la invitación pendiente no puede
aprobar nada, así que contarlo congelaría las elevaciones sin nadie capaz de destrabarlas.

El cierre se evalúa PEREZOSAMENTE —en cada decisión de elevación, en ``/auth/me``, al aceptar
una invitación y al arrancar— y es un UPDATE condicional: con dos requests a la vez cierra uno y
audita uno.

CÓMO SE ABRE
------------
- La migración ``b1d3f5a7c9e2`` deja la fila POR ABRIR si la instalación tiene ≤ 1
  ``access_admin``, y cerrada (``multiple_admins_at_upgrade``) si tiene más.
- El primer arranque la abre (``startup``): el plazo corre desde ese arranque, no desde la
  migración. Un esquema creado con ``create_all`` (tests, desarrollo) no tiene fila: el arranque
  la crea con el mismo criterio.
- La siembra de una instalación vacía y ``ADMIN_RECOVERY=1`` la REABREN con plazo nuevo
  (``reopen``). Nada más la reabre.

FALLA CERRADO
-------------
Sin la tabla (código antes que la migración, un downgrade) o ante cualquier error de lectura,
``applies_to`` es ``False``: las elevaciones quedan pendientes, que es el comportamiento de C3. La
ventana nunca se abre por un error.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from app.core.environments import ACCESS_BOOTSTRAP_WINDOW_HOURS
from app.models.access_bootstrap import (
    CLOSED_DEADLINE,
    CLOSED_MULTIPLE_ADMINS,
    CLOSED_SECOND_ADMIN,
)
from app.models.access_bootstrap_model import AccessBootstrapModel, utcnow
from app.models.user_model import UserModel
from app.services import audit

logger = logging.getLogger(__name__)

ACTION_ASSIGNMENT = "access.bootstrap_assignment"
ACTION_OPENED = "access.bootstrap_window_opened"
ACTION_CLOSED = "access.bootstrap_window_closed"


def _now() -> datetime:
    """El reloj de la ventana. Los tests lo cambian con ``monkeypatch``."""
    return utcnow()


def window_hours() -> int:
    """El valor vigente de ``ACCESS_BOOTSTRAP_WINDOW_HOURS``. Los tests lo cambian."""
    return int(ACCESS_BOOTSTRAP_WINDOW_HOURS)


def _deadline(now: datetime) -> datetime:
    return now + timedelta(hours=window_hours())


def _admins() -> int:
    return UserModel().count_active_access_admins()


def is_open(row: dict | None, now: datetime | None = None) -> bool:
    """Abierta = abierta, sin cerrar y sin vencer. Una fila por abrir NO está abierta."""
    if not row or row.get("opened_at") is None or row.get("closed_at") is not None:
        return False
    closes_at = row.get("closes_at")
    return closes_at is not None and (now or _now()) < closes_at


def _audit(action: str, detail: dict) -> None:
    audit.record(
        action,
        admin=None,
        actor_type="system",
        target_type="access_bootstrap",
        target_id=1,
        touched_engine=False,
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )


# --------------------------------------------------------------------------- #
# Lectura con cierre perezoso                                                  #
# --------------------------------------------------------------------------- #
def refresh(now: datetime | None = None) -> dict | None:
    """
    La fila, ya cerrada si le tocaba. ``None`` sin tabla o sin fila (falla cerrado).

    Cierra por ``deadline`` antes que por ``second_admin``: si las dos son ciertas, el plazo ya
    había vencido antes, y el motivo tiene que contar lo que pasó primero.
    """
    now = now or _now()
    model = AccessBootstrapModel()
    try:
        row = model.get()
        if not row or row["opened_at"] is None or row["closed_at"] is not None:
            return row
        reason = None
        if row["closes_at"] is None or now >= row["closes_at"]:
            reason = CLOSED_DEADLINE
        elif _admins() >= 2:
            reason = CLOSED_SECOND_ADMIN
        if reason is None:
            return row
        if model.close(opened_at=row["opened_at"], closed_at=now, reason=reason):
            _audit(ACTION_CLOSED, {"reason": reason, "opened_at": row["opened_at"],
                                   "closes_at": row["closes_at"], "closed_at": now})
            logger.info("Ventana de arranque de accesos CERRADA (%s).", reason)
        return model.get()
    except Exception as exc:  # noqa: BLE001 — sin tabla o sin BD: no existe (falla cerrado)
        # Sin traceback: con la tabla sin migrar esto corre en cada `/auth/me`.
        logger.warning("No se pudo leer la ventana de arranque (%s); se trata como cerrada.",
                       type(exc).__name__)
        return None


def applies_to(actor) -> bool:
    """
    ¿Las elevaciones de ``actor`` se aplican YA por la ventana de arranque?

    Sí solo si la ventana está abierta Y ``actor`` es el ÚNICO ``access_admin`` activo con
    credencial. El segundo chequeo no es redundante con el cierre por ``second_admin``: es lo
    que impide que una sesión que no es la del administrador único (un token, otra cuenta que se
    coló) herede la excepción. Falla cerrado.
    """
    from app.core.actor import Actor

    if not isinstance(actor, Actor) or actor.kind != "admin" or actor.id is None:
        return False
    try:
        now = _now()
        if not is_open(refresh(now), now):
            return False
        users = UserModel()
        return (
            users.count_active_access_admins() == 1
            and users.count_active_access_admins(exclude_user_id=actor.id) == 0
        )
    except Exception:  # noqa: BLE001 — ante la duda, segundo aprobador
        logger.warning("No se pudo evaluar la ventana de arranque; se exige segundo aprobador.",
                       exc_info=True)
        return False


def state_for(actor) -> dict | None:
    """
    ``/auth/me.bootstrap_window``: ``{open, closes_at}`` para quien tiene ``access.admin``;
    ``None`` para el resto, sin fila o sin tabla. Es lo que la SPA usa para el banner.
    """
    from app.services.capability_catalog import Capability

    if getattr(actor, "kind", None) != "admin" or not actor.has(Capability.ACCESS_ADMIN_CAP):
        return None
    now = _now()
    row = refresh(now)
    if not row:
        return None
    return {"open": is_open(row, now), "closes_at": row["closes_at"]}


# --------------------------------------------------------------------------- #
# Apertura                                                                     #
# --------------------------------------------------------------------------- #
def startup() -> dict | None:
    """
    El arranque: crea la fila si falta, abre la que dejó la migración por abrir, cierra la que
    venció o ya tiene segundo administrador, y avisa si queda abierta. No lanza.
    """
    now = _now()
    model = AccessBootstrapModel()
    try:
        row = model.get()
        if row is None:
            if _admins() <= 1:
                if model.insert(opened_at=now, closes_at=_deadline(now)):
                    _audit(ACTION_OPENED, {"reason": "first_boot", "closes_at": _deadline(now)})
            else:
                model.insert(opened_at=None, closes_at=None, closed_at=now,
                             closed_reason=CLOSED_MULTIPLE_ADMINS)
        elif row["opened_at"] is None and row["closed_at"] is None:
            if model.open_pending(opened_at=now, closes_at=_deadline(now)):
                _audit(ACTION_OPENED, {"reason": "first_boot", "closes_at": _deadline(now)})
    except Exception:  # noqa: BLE001 — la ventana no puede impedir el arranque
        logger.exception("No se pudo inicializar la ventana de arranque; queda cerrada.")
        return None
    row = refresh(now)
    if is_open(row, now):
        logger.warning(
            "Ventana de arranque de accesos ABIERTA hasta %s UTC: el único access_admin eleva "
            "sin segundo aprobador (auditado access.bootstrap_assignment). Se cierra cuando un "
            "segundo access_admin acepte su invitación.",
            row["closes_at"].isoformat(timespec="seconds"),
        )
    return row


def reopen(*, reason: str) -> dict | None:
    """
    Abre (o reabre) la ventana con un plazo nuevo desde ahora. Solo la llaman la siembra de una
    instalación vacía y ``ADMIN_RECOVERY=1`` (``app/core/auth.py``). Lanza si no puede: quien la
    llama decide si eso impide algo.
    """
    now = _now()
    closes_at = _deadline(now)
    AccessBootstrapModel().reopen(opened_at=now, closes_at=closes_at)
    _audit(ACTION_OPENED, {"reason": reason, "closes_at": closes_at})
    return AccessBootstrapModel().get()
