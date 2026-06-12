"""
Servicio de auditoría: registra operaciones sensibles en la tabla ``audit_log``.

Principios:
- **Best-effort:** un fallo al auditar NUNCA debe romper la operación de negocio.
- **Sin secretos:** ``detail`` es un resumen corto; jamás se escriben credenciales,
  passwords ni datos de filas.
- Toma Request ID e IP de los ContextVars de la request cuando están disponibles.
"""

from contextvars import ContextVar

from app.core.context import current_http_identifier, current_request_ip
from app.core.database import Database
from app.core.logger import get_logger
from app.models.audit_log import AuditLog

logger = get_logger(__name__)


def _safe_get(ctxvar: ContextVar) -> str | None:
    try:
        value = ctxvar.get()
    except LookupError:
        return None
    return value or None


def record(
    action: str,
    *,
    status: str = "success",
    admin: dict | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    server_id: int | None = None,
    touched_engine: bool = False,
    detail: str | None = None,
) -> None:
    """Registra una entrada de auditoría. Nunca lanza: ante error, solo loguea."""
    try:
        admin = admin or {}
        session = Database().get_declarative_base_session()
        try:
            session.add(
                AuditLog(
                    request_id=_safe_get(current_http_identifier),
                    admin_id=admin.get("id"),
                    admin_username=admin.get("username"),
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    server_id=server_id,
                    touched_engine=touched_engine,
                    status=status,
                    detail=(detail or None),
                    ip=_safe_get(current_request_ip),
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — auditar nunca debe romper la operación
        logger.warning(
            "No se pudo registrar auditoría para action=%s", action, exc_info=True
        )
