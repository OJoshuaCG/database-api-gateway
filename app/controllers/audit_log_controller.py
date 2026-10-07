"""
Lectura de ``audit_log`` (``GET /audit-log``), detrás de ``audit.read``.

POR QUÉ ``audit.read`` Y NO ``access.admin``
--------------------------------------------
La separación de deberes se apoya en que toda escalada de un solo actor queda auditada, y eso
solo es un control si alguien que NO la hizo puede leerla. ``access_admin`` hace los cambios de
acceso: si también leyera el rastro, el revisado se revisaría a sí mismo. Por eso la lectura es de
``security_officer`` (la única global con ``audit.read``), y ``access_admin`` recibe 403.

POR QUÉ NO DIVULGA (``discloses=False``) Y POR QUÉ EL ``detail`` SE ENMASCARA
-----------------------------------------------------------------------------
``discloses`` marca lo que saca DATOS DEL TERCERO del perímetro (filas de negocio, credenciales
del motor). La auditoría dice quién hizo qué, sobre qué objeto y con qué resultado. El caso límite
es ``query_console.execute``, cuyo ``detail`` lleva hasta 500 caracteres del SQL con los secretos
redactados (``query_policy.redact_secrets``) pero CON los literales. El historial de la consola SQL
ya enmascara esos literales para quien no tiene ``sql_console.execute``
(``QueryConsoleController.list_history``); sin el mismo tratamiento acá, ``audit.read`` era un
camino lateral para leer lo que el historial esconde. Por eso las filas ``query_console.*`` pasan
por ``sql_masking.mask_literals`` salvo que el lector pueda ejecutar SQL en el destino de la fila
(``security_officer`` nunca puede: ``sql_console.execute`` es de ``owner``). Con ese enmascarado la
lectura sigue sin divulgar, y un ``GET`` no pide step-up (la regla de método de
``app/core/step_up.py``).

El enmascarado es fail-closed: de una fila ``query_console.*`` solo se conserva tal cual el prefijo
conocido de la intención de ejecución (``<bd> as <usuario> (<modo>) [<peligro>]: ``); cualquier otro
formato se enmascara entero, y sin servidor identificable (o con un servidor que ya no existe) se
usa el dialecto por defecto de ``mask_literals``.

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
from typing import TYPE_CHECKING

from app.controllers.common import engine_value
from app.core.database import Database
from app.core.scope import can_at
from app.core.scope_targets import sql_console_for
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.models.server import Server
from app.services.capability_catalog import Capability
from app.services.db_admin import sql_masking

if TYPE_CHECKING:
    from app.core.actor import Actor

CODE_NOT_FOUND = "audit.not_found"
CODE_INVALID_RANGE = "audit.invalid_range"

_LIKE_ESCAPE = "\\"

#: Las acciones de la consola SQL. Solo ``query_console.execute`` (la intención previa al motor)
#: lleva SQL hoy, pero el prefijo entero se trata como sensible: un formato nuevo no abre la fuga.
QUERY_CONSOLE_ACTION_PREFIX = "query_console."

#: Cierra el prefijo estructurado de la intención de ejecución y abre el SQL:
#: ``<bd> as <usuario> (<modo>) [<peligro>]: <sql>``. Es la PRIMERA aparición: un nombre de base o
#: de usuario que contenga ``]: `` solo hace que se enmascare de más, nunca de menos.
_INTENT_SQL_SEPARATOR = "]: "


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
            "detail_masked": False,
            "ip": f.ip,
            "grantee": f.grantee,
            "privilege": f.privilege,
            "object_level": f.object_level,
            "object_name": f.object_name,
            "with_grant_option": f.with_grant_option,
            "grantor": f.grantor,
        }

    @staticmethod
    def _engines_by_server(server_ids: set[int]) -> dict[int, str]:
        """Dialecto de cada servidor pedido, para que el enmascarado use las reglas de su motor."""
        if not server_ids:
            return {}
        session = AuditLogController._session()
        try:
            servers = session.query(Server).filter(Server.id.in_(server_ids)).all()
            return {server.id: engine_value(server) for server in servers}
        finally:
            session.close()

    @staticmethod
    def _mask_console_detail(detail: str | None, engine: str) -> str | None:
        """``detail`` de una fila ``query_console.*`` con los literales del SQL reemplazados por ``?``."""
        if not detail:
            return detail
        separator_at = detail.find(_INTENT_SQL_SEPARATOR)
        if separator_at == -1:
            return sql_masking.mask_literals(detail, engine)
        sql_starts_at = separator_at + len(_INTENT_SQL_SEPARATOR)
        structured_prefix = detail[:sql_starts_at]
        sql_text = detail[sql_starts_at:]
        return structured_prefix + sql_masking.mask_literals(sql_text, engine)

    @classmethod
    def _apply_detail_masking(cls, entries: list[dict], *, reader: "Actor") -> None:
        """
        Enmascara EN SITIO el ``detail`` de las filas ``query_console.*`` que el lector no podría
        haber visto completas, y marca cada fila con ``detail_masked``.

        La decisión usa la misma regla que el historial de la consola: ``sql_console.execute`` en
        el destino de la fila (``can_at``), y se cachea por servidor porque la capa 2 puede ir a
        la BD. Una fila sin servidor no tiene destino al que anclar el permiso: se enmascara.
        """
        console_entries = [
            entry
            for entry in entries
            if str(entry["action"]).startswith(QUERY_CONSOLE_ACTION_PREFIX)
        ]
        for entry in entries:
            entry["detail_masked"] = False
        if not console_entries:
            return

        may_see_full_sql_at: dict[int | None, bool] = {}
        for entry in console_entries:
            server_id = entry["server_id"]
            if server_id not in may_see_full_sql_at:
                may_see_full_sql_at[server_id] = (
                    server_id is not None
                    and can_at(
                        reader,
                        Capability.SQL_CONSOLE_EXECUTE,
                        sql_console_for(server_id, None),
                    )
                )

        masked_entries = [
            entry for entry in console_entries if not may_see_full_sql_at[entry["server_id"]]
        ]
        server_ids_to_resolve = {
            entry["server_id"] for entry in masked_entries if entry["server_id"] is not None
        }
        engine_by_server_id = cls._engines_by_server(server_ids_to_resolve)
        for entry in masked_entries:
            engine = engine_by_server_id.get(entry["server_id"], "")
            masked_detail = cls._mask_console_detail(entry["detail"], engine)
            entry["detail"] = masked_detail
            entry["detail_json"] = _parse_detail(masked_detail)
            entry["detail_masked"] = True

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

    def list_entries(
        self, filters: dict, *, limit: int, offset: int, reader: "Actor"
    ) -> tuple[list[dict], int]:
        """
        ``reader`` es keyword y sin default, como en ``QueryConsoleController.list_history``: una
        llamada que olvide decir quién lee falla con ``TypeError`` en vez de devolver el SQL
        completo de la consola.
        """
        session = self._session()
        try:
            q = self._filtered(session.query(AuditLog), filters)
            total = q.count()
            filas = q.order_by(AuditLog.id.desc()).limit(limit).offset(offset).all()
            entries = [self._serialize(f) for f in filas]
        finally:
            session.close()
        self._apply_detail_masking(entries, reader=reader)
        return entries, total

    def get_entry(self, entry_id: int, *, reader: "Actor") -> dict:
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
            entry = self._serialize(fila)
        finally:
            session.close()
        self._apply_detail_masking([entry], reader=reader)
        return entry
