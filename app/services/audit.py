"""
Servicio de auditoría: registra operaciones sensibles en la tabla ``audit_log``.

Principios:
- **Best-effort** (``record``): un fallo al auditar NUNCA debe romper la operación
  de negocio.
- **Fail-closed** (``record_intent``): para operaciones destructivas/GATE (REVOKE,
  ``WITH GRANT OPTION``, ``CASCADE``…) se registra la INTENCIÓN antes de tocar el
  motor; si esa auditoría no se persiste, se ABORTA la operación (no es best-effort).
- **Sin secretos:** ``detail`` es un resumen corto; jamás se escriben credenciales,
  passwords ni datos de filas.
- Toma Request ID e IP de los ContextVars de la request cuando están disponibles.
"""

from contextvars import ContextVar

from app.core.context import current_http_identifier, current_request_ip
from app.core.database import Database
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog

logger = get_logger(__name__)


def _safe_get(ctxvar: ContextVar) -> str | None:
    try:
        value = ctxvar.get()
    except LookupError:
        return None
    return value or None


def _build(
    action: str,
    *,
    status: str,
    admin: dict | None,
    target_type: str | None,
    target_id: int | None,
    server_id: int | None,
    touched_engine: bool,
    detail: str | None,
    grantee: str | None,
    privilege: str | None,
    object_level: str | None,
    object_name: str | None,
    with_grant_option: bool | None,
    grantor: str | None,
) -> AuditLog:
    admin = admin or {}
    return AuditLog(
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
        grantee=grantee,
        privilege=privilege,
        object_level=object_level,
        object_name=object_name,
        with_grant_option=with_grant_option,
        grantor=grantor,
    )


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
    grantee: str | None = None,
    privilege: str | None = None,
    object_level: str | None = None,
    object_name: str | None = None,
    with_grant_option: bool | None = None,
    grantor: str | None = None,
) -> None:
    """Registra una entrada de auditoría. Nunca lanza: ante error, solo loguea."""
    try:
        session = Database().get_declarative_base_session()
        try:
            session.add(
                _build(
                    action,
                    status=status,
                    admin=admin,
                    target_type=target_type,
                    target_id=target_id,
                    server_id=server_id,
                    touched_engine=touched_engine,
                    detail=detail,
                    grantee=grantee,
                    privilege=privilege,
                    object_level=object_level,
                    object_name=object_name,
                    with_grant_option=with_grant_option,
                    grantor=grantor,
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — auditar nunca debe romper la operación
        logger.warning(
            "No se pudo registrar auditoría para action=%s", action, exc_info=True
        )


def record_intent(
    action: str,
    *,
    admin: dict | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    server_id: int | None = None,
    detail: str | None = None,
    grantee: str | None = None,
    privilege: str | None = None,
    object_level: str | None = None,
    object_name: str | None = None,
    with_grant_option: bool | None = None,
    grantor: str | None = None,
) -> None:
    """
    Registra la INTENCIÓN (``status="attempt"``) de una operación destructiva/GATE
    ANTES de ejecutarla. **Fail-closed**: si no se puede persistir, lanza
    ``AppHttpException(500)`` y la operación NO debe continuar.

    Garantiza un rastro durable incluso si la operación posterior corrompe el proceso
    a mitad de camino.
    """
    try:
        session = Database().get_declarative_base_session()
        try:
            session.add(
                _build(
                    action,
                    status="attempt",
                    admin=admin,
                    target_type=target_type,
                    target_id=target_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail=detail,
                    grantee=grantee,
                    privilege=privilege,
                    object_level=object_level,
                    object_name=object_name,
                    with_grant_option=with_grant_option,
                    grantor=grantor,
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Auditoría de intención falló para action=%s; se aborta la operación",
            action,
            exc_info=True,
        )
        raise AppHttpException(
            message=(
                "No se pudo registrar la auditoría de intención de la operación; "
                "se aborta por política fail-closed."
            ),
            status_code=500,
            context={"action": action},
        ) from exc
