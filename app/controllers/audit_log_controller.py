"""
Lectura de ``audit_log`` (``GET /audit-log``), detrás de ``policy.admin``.

POR QUÉ ``policy.admin`` Y NO ``access.admin``
---------------------------------------------
La separación de deberes se apoya en que toda escalada de un solo actor queda auditada, y eso
solo es un control si alguien que NO la hizo puede leerla. ``access_admin`` hace los cambios de
acceso: si también leyera el rastro, el revisado se revisaría a sí mismo. Por eso la lectura es de
``security_officer`` (la única global con ``policy.admin``), y ``access_admin`` recibe 403.

POR QUÉ NO DIVULGA (``discloses=False``)
---------------------------------------
``discloses`` marca lo que saca DATOS DEL TERCERO del perímetro (filas de negocio, credenciales
del motor). La auditoría dice quién hizo qué, sobre qué objeto y con qué resultado. El caso límite
es ``query_console.execute``, cuyo ``detail`` lleva hasta 500 caracteres del SQL con los secretos
redactados (``query_policy.redact_secrets``) pero con los literales: es la misma información que
``sql_console.history`` ya le muestra a ``viewer``, que el catálogo clasifica como no divulgante.
Marcar la auditoría como divulgante sería incoherente con eso. Consecuencia: un ``GET`` no pide
step-up (la regla de método de ``app/core/step_up.py``).

ORDEN Y FILTROS
---------------
Más nuevas primero por ``id DESC``: la PK es monótona con la inserción, así que el orden es
estable entre páginas sin depender de la resolución de ``created_at`` (en MySQL, segundos: dos
filas del mismo segundo se intercambiarían entre requests con un ``ORDER BY created_at``).

``action`` es igualdad exacta, o prefijo si termina en ``*`` (``access.*``). El prefijo se
traduce a ``LIKE`` con ``%`` y ``_`` ESCAPADOS: ``_`` es un comodín de ``LIKE`` y aparece en casi
todas las acciones (``gateway_user.*`` sin escapar matchearía ``gatewayXuser.create``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog

CODE_NOT_FOUND = "audit.not_found"
CODE_INVALID_RANGE = "audit.invalid_range"

_LIKE_ESCAPE = "\\"


def _like_prefix(prefix: str) -> str:
    escapado = (
        prefix.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return escapado + "%"


def _naive_utc(value: datetime | None) -> datetime | None:
    """Las columnas son ``DATETIME`` sin zona (UTC). Un valor con zona se pasa a UTC naive."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_detail(detail: str | None):
    """``detail`` como JSON si lo es (objeto o lista); ``None`` si es texto libre o vacío."""
    if not detail:
        return None
    texto = detail.lstrip()
    if not texto or texto[0] not in "{[":
        return None
    try:
        return json.loads(detail)
    except ValueError:
        return None


class AuditLogController:
    @staticmethod
    def _session():
        return Database().get_declarative_base_session()

    @staticmethod
    def _serialize(f: AuditLog) -> dict:
        return {
            "id": f.id,
            "created_at": f.created_at,
            "request_id": f.request_id,
            "actor_type": f.actor_type,
            "admin_id": f.admin_id,
            "admin_username": f.admin_username,
            "api_token_id": f.api_token_id,
            "action": f.action,
            "target_type": f.target_type,
            "target_id": f.target_id,
            "server_id": f.server_id,
            "touched_engine": bool(f.touched_engine),
            "status": f.status,
            "detail": f.detail,
            "detail_json": _parse_detail(f.detail),
            "ip": f.ip,
            "grantee": f.grantee,
            "privilege": f.privilege,
            "object_level": f.object_level,
            "object_name": f.object_name,
            "with_grant_option": f.with_grant_option,
            "grantor": f.grantor,
        }

    @staticmethod
    def _filtered(q, filters: dict):
        action = filters.get("action")
        if action:
            if action.endswith("*"):
                prefijo = action[:-1]
                if prefijo:
                    q = q.filter(
                        AuditLog.action.like(_like_prefix(prefijo), escape=_LIKE_ESCAPE)
                    )
            else:
                q = q.filter(AuditLog.action == action)
        for campo in (
            "admin_id",
            "admin_username",
            "actor_type",
            "api_token_id",
            "target_type",
            "target_id",
            "server_id",
            "status",
            "request_id",
        ):
            valor = filters.get(campo)
            if valor is not None and valor != "":
                q = q.filter(getattr(AuditLog, campo) == valor)
        desde = _naive_utc(filters.get("date_from"))
        hasta = _naive_utc(filters.get("date_to"))
        if desde is not None and hasta is not None and desde >= hasta:
            raise AppHttpException(
                message="El rango de fechas está vacío: 'from' tiene que ser anterior a 'to'.",
                status_code=422,
                public_context={"code": CODE_INVALID_RANGE},
            )
        if desde is not None:
            q = q.filter(AuditLog.created_at >= desde)
        if hasta is not None:
            q = q.filter(AuditLog.created_at < hasta)
        return q

    def list_entries(self, filters: dict, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            q = self._filtered(session.query(AuditLog), filters)
            total = q.count()
            filas = q.order_by(AuditLog.id.desc()).limit(limit).offset(offset).all()
            return [self._serialize(f) for f in filas], total
        finally:
            session.close()

    def get_entry(self, entry_id: int) -> dict:
        session = self._session()
        try:
            fila = session.get(AuditLog, entry_id)
            if fila is None:
                raise AppHttpException(
                    message="Entrada de auditoría no encontrada.",
                    status_code=404,
                    public_context={"code": CODE_NOT_FOUND},
                    context={"audit_id": entry_id},
                )
            return self._serialize(fila)
        finally:
            session.close()
