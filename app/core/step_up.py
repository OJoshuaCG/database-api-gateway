"""
Step-up ("sudo mode"): las capacidades con ``requires_step_up`` exigen una contraseña FRESCA.

QUÉ ES LA VENTANA
-----------------
``gateway_sessions.step_up_at`` + ``STEP_UP_TTL_SECONDS``. La abre el login (una contraseña
recién tipeada es una contraseña fresca) y la renueva ``POST /auth/step-up``. Tres decisiones,
cada una con el modo de fallo que evita:

- **Por tiempo, no de un solo uso.** Los flujos sensibles mandan varios requests (preview →
  execute del DROP y de la consola, alta → ejecución del clonado, apply → stamp, lotes,
  reintentos de la SPA). Un permiso de un solo uso pediría la contraseña en el medio de cada uno.
- **No atada a la capacidad ni al destino.** Dentro de 5 minutos eso es fricción sin protección:
  el binding operación ↔ destino ya lo dan los ``confirm_token``.
- **No deslizante.** Usarla no la estira: una cookie robada dentro de la ventana no la puede
  mantener abierta con actividad.

CUÁNDO SE PIDE
--------------
Con ``STEP_UP_ENFORCED`` y ``spec(cap).requires_step_up``, y además:

- método NO seguro (todo lo que no es GET/HEAD/OPTIONS), **o**
- la capacidad DIVULGA (``exports/{id}/content``, capturas: un GET que entrega datos), **o**
- el método es desconocido (llamada fuera de un request): fail-closed.

Así listar usuarios (``gateway.admin``) o versiones de blueprint (``blueprints.apply``) por GET
no interrumpe a nadie, y bajar datos sí.

EL ORDEN, Y POR QUÉ ESTE ES EL ÚLTIMO CHEQUEO
---------------------------------------------
Capa 1 (403 ``access.forbidden``) → capa 2 (mismo 403) → step-up. Nadie tiene que tipear su
contraseña para enterarse después de que igual no podía. Y el 403 se levanta en la dependencia
(o en el guard al tope del handler), **antes de cualquier efecto**: reintentar tras el prompt es
seguro — en particular ``GET .../content`` no consume el artefacto con este 403.

LOS TOKENS DE AGENTE NUNCA LLEGAN ACÁ
-------------------------------------
El techo de agente no tiene ninguna capacidad con step-up (invariante 11 del catálogo) y un
token no tiene contraseña que reconfirmar. Si igual llegara uno, es 403 ``access.forbidden``:
pedirle un step-up a una máquina es un prompt que nadie puede contestar.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.actor import Actor
from app.core.environments import STEP_UP_ENFORCED, STEP_UP_TTL_SECONDS
from app.exceptions import AppHttpException
from app.services.capability_catalog import (
    CODE_FORBIDDEN,
    CODE_STEP_UP_REQUIRED,
    Capability,
    spec,
)

__all__ = [
    "STEP_UP_ENFORCED",
    "STEP_UP_TTL_SECONDS",
    "assert_step_up",
    "enforced",
    "is_fresh",
    "window_until",
]

#: Métodos que no piden step-up salvo que la capacidad divulgue. Todo lo demás —incluido un
#: método desconocido— lo pide.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _now() -> datetime:
    # El mismo reloj que la sesión: un test que adelanta ``session_store._utcnow`` vence las dos
    # cosas juntas, que es lo que pasa en la realidad.
    from app.core import session_store

    return session_store._utcnow()


def window_until(step_up_at: datetime | None) -> datetime | None:
    """Fin de la ventana que abrió ``step_up_at``, o ``None`` si no hay ninguna."""
    if step_up_at is None:
        return None
    return step_up_at + timedelta(seconds=STEP_UP_TTL_SECONDS)


def is_fresh(actor: Actor) -> bool:
    """¿La ventana de step-up del actor sigue abierta AHORA?"""
    return actor.step_up_until is not None and _now() < actor.step_up_until


def _method_requires(capability: Capability, method: str | None) -> bool:
    if method is None:
        return True  # fuera de un request: fail-closed
    if method.upper() not in _SAFE_METHODS:
        return True
    return spec(capability).discloses


def assert_step_up(actor: Actor, capability: Capability, *, method: str | None = None) -> None:
    """
    403 ``access.step_up_required`` si ``capability`` pide step-up y la ventana no está abierta.

    ``method`` es el del request; si no se pasa se lee de ``current_request_method`` (lo fija el
    ``ContextMiddleware``). Va DESPUÉS de las capas 1 y 2: ver el docstring del módulo.

    ``public_context`` lleva ``step_up_ttl_seconds`` para que la SPA pueda decir cuánto dura la
    confirmación sin hardcodearlo.
    """
    if not spec(capability).requires_step_up:
        return
    if actor.is_agent:
        # Inalcanzable por el invariante 11; escrito igual. Ver el docstring del módulo.
        raise AppHttpException(
            message="No tienes permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_FORBIDDEN},
        )
    # Global del módulo, leída en cada llamada: un test lo apaga con
    # ``monkeypatch.setattr(step_up, "STEP_UP_ENFORCED", False)``.
    if not STEP_UP_ENFORCED:
        return
    if method is None:
        from app.core.context import current_request_method

        method = current_request_method.get()
    if not _method_requires(capability, method):
        return
    if is_fresh(actor):
        return
    raise AppHttpException(
        message="Esta operación requiere confirmar tu contraseña.",
        status_code=403,
        public_context={
            "code": CODE_STEP_UP_REQUIRED,
            "step_up_ttl_seconds": STEP_UP_TTL_SECONDS,
        },
    )


def enforced() -> bool:
    """El valor vigente de ``STEP_UP_ENFORCED`` (el que lee ``assert_step_up``)."""
    return bool(STEP_UP_ENFORCED)
