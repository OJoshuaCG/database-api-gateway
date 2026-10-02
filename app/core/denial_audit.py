"""
Rastro de las DENEGACIONES de acceso: 403 por capacidad, por alcance y por CSRF/Origin.

Antes, esos 403 se levantaban sin dejar fila: un ``viewer`` probando
``DELETE …?drop_remote=true`` sobre producción, un ``access_admin`` tanteando rutas operativas o
una campaña cross-site contra la cookie de un administrador eran indetectables. Solo las
denegaciones de tools del MCP se auditaban (``app/mcp/dispatch.py``).

TRES PROPIEDADES, NINGUNA OPCIONAL
----------------------------------
- **Nunca cambia el 403.** Todo el registro va dentro de un ``try`` que solo loguea: un fallo
  al auditar no puede convertirse en un 500 ni, peor, en un 200. El 403 lo levanta el llamador
  después de llamar acá, pase lo que pase.
- **No filtra nada al cliente.** La capacidad que faltó va al ``detail`` de ``audit_log`` (lo lee
  el operador) y jamás a la respuesta, que sigue siendo el ``access.forbidden`` opaco.
- **Agregado.** Como mucho una fila por ``(actor, código, método, ruta)`` por ventana, con la
  cuenta de lo que quedó sin fila (``WindowedAggregator``). Sin eso, un actor autenticado con un
  bucle de 403 escribía en ``audit_log`` gratis.

LA RUTA SE NORMALIZA
--------------------
La clave usa el path con los segmentos numéricos/opacos reemplazados por ``{id}``: sin eso, un
sondeo que recorre ``/servers/1``, ``/servers/2``… estrenaría una clave —y una fila— por id. El
path crudo de la primera denegación sí va en el ``detail``.

LOS TOKENS NO PASAN POR ACÁ
---------------------------
Un actor ``api_token`` solo llega a estos chequeos desde el dispatch del MCP, que ya audita la
denegación de la tool con su propia fila. Registrarla dos veces duplicaría el evento.
"""

from __future__ import annotations

import json
import re

from app.core.audit_aggregator import WindowedAggregator
from app.core.logger import get_logger

logger = get_logger(__name__)

ACTION = "access.denied"

#: Ventana de agregación: como mucho una fila por clave por ventana.
DENIAL_AUDIT_WINDOW_SECONDS = 60.0
#: Tope de claves recordadas: el agregador que existe para acotar un recurso no puede ser otro
#: sin cota.
DENIAL_AUDIT_MAX_KEYS = 10_000
#: Tope del path y del origin que se copian al detalle: son texto que manda el cliente.
_MAX_FIELD = 256

_denials = WindowedAggregator(
    window=DENIAL_AUDIT_WINDOW_SECONDS, max_keys=DENIAL_AUDIT_MAX_KEYS
)

# Un segmento que es un id (entero, uuid/hex largo o token URL-safe largo), no parte de la ruta.
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F-]{16,}|[A-Za-z0-9_-]{24,})$")


def reset_denial_state() -> None:
    """Olvida el agregador. Para los tests: vive lo que el proceso."""
    _denials.reset()


def normalize_route(path: str | None) -> str | None:
    """``/api/v1/servers/3/users`` → ``/api/v1/servers/{id}/users``."""
    if not path:
        return None
    return "/".join(
        "{id}" if seg and _ID_SEGMENT.match(seg) else seg for seg in path.split("/")
    )[:_MAX_FIELD]


def _ctx(var) -> str | None:
    try:
        return var.get() or None
    except LookupError:
        return None


def record_denial(
    code: str,
    *,
    actor=None,
    capability=None,
    check: str,
    extra: dict | None = None,
) -> None:
    """
    Registra (agregado) una denegación. **Nunca lanza.**

    ``check`` dice qué guard negó (``capability`` | ``scope`` | ``csrf``): es para el operador,
    nunca para la respuesta. ``capability`` puede ser un ``Capability`` o ``None`` (CSRF).
    """
    try:
        if getattr(actor, "kind", None) == "api_token":
            return
        from app.core.actor import actor_type_of, identity_of
        from app.core.context import (
            current_request_ip,
            current_request_method,
            current_request_route,
        )

        method = _ctx(current_request_method)
        path = _ctx(current_request_route)
        route = normalize_route(path)
        actor_id, _ = identity_of(actor)
        # Sin id de actor (no debería pasar: estos guards corren ya autenticados) se agrupa por
        # IP, para no colapsar a todos los anónimos en una sola clave.
        quien = actor_id if actor_id is not None else f"ip:{_ctx(current_request_ip)}"
        cap = getattr(capability, "value", capability)
        agregados = _denials.admit((actor_type_of(actor), quien, code, method, route))
        if agregados is None:
            return

        detail = {
            "code": code,
            "check": check,
            "capability": cap,
            "method": method,
            "route": route,
            "path": (path or "")[:_MAX_FIELD] or None,
            "aggregated": agregados,
            "window_seconds": int(_denials.window),
        }
        for k, v in (extra or {}).items():
            detail[k] = v[:_MAX_FIELD] if isinstance(v, str) else v

        from app.services import audit

        audit.record(
            ACTION,
            status="failure",
            admin=actor,
            actor_type="anonymous" if actor is None else None,
            target_type="route",
            touched_engine=False,
            privilege=cap,
            detail=json.dumps(detail, ensure_ascii=False),
        )
    except Exception:  # noqa: BLE001 — auditar una denegación nunca cambia la denegación
        logger.warning("No se pudo auditar la denegación %s", code, exc_info=True)
