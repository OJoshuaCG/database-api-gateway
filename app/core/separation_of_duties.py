"""
Regla de SEPARACIÓN DE DEBERES sobre una cuenta del gateway. Pura: no toca la BD.

LA REGLA
--------
Una cuenta con ``security_officer`` (escribe la política: entornos, catálogos, servidores, crypto)
no puede tener además:

- ``owner`` en ninguna forma (``SOD_RULE_OWNER``): rol base ``owner``, un rol ``owner`` por
  alcance, o una capacidad puntual de ``OWNER_ONLY_CAPABILITIES``. Las tres son ``owner`` en
  sustancia: quien opera producción no puede ser quien apaga la barrera de producción.
- ``access_admin`` (``SOD_RULE_ACCESS_ADMIN``): las dos globales juntas reconstruyen el
  administrador combinado que la partición de ``gateway.admin`` vino a deshacer.

DÓNDE SE APLICA
---------------
- **Al escribir** (``app/services/sod_service.py``): ``create_user``, ``update_user``,
  ``set_access`` y el alta/aprobación de capacidades puntuales responden 409
  ``access.sod_conflict`` si el estado RESULTANTE viola una regla que ninguna excepción viva
  cubre, salvo ``sod_override`` (break-glass auditado).
- **Al leer** (``parse_access_context``): si la combinación está presente sin excepción viva que
  la cubra —una fila editada a mano, una excepción vencida—, se descartan las capacidades de
  ``security_officer`` (falla cerrado). ``owner`` y ``access_admin`` se conservan: quitar
  ``access_admin`` podría dejar al gateway sin nadie que lo repare, y ``owner`` es el trabajo
  diario de la persona; la política es lo que se cae.

``admin_actor`` NO aplica la regla: es el constructor de un ``Actor`` con la política ya
resuelta, y lo usan tests y caminos que no leen la BD. La regla vive en el LECTOR
(``parse_access_context``), que es donde se conocen las excepciones.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.capability_catalog import (
    OWNER_ONLY_CAPABILITIES,
    SOD_RULE_ACCESS_ADMIN,
    SOD_RULE_OWNER,
    GatewayRole,
    GlobalCapability,
)

_OWNER_ONLY_VALUES: frozenset[str] = frozenset(c.value for c in OWNER_ONLY_CAPABILITIES)


def _v(x) -> str:
    return str(getattr(x, "value", x))


def conflicts(
    *,
    base_role,
    scope_roles: Iterable[tuple],
    globals_: Iterable,
    capabilities: Iterable[tuple] = (),
) -> dict[str, list[dict]]:
    """
    ``{regla: [fuentes]}`` de las reglas que el estado viola. Vacío si no viola ninguna.

    ``scope_roles``: ``(scope_type, scope_id, role)``. ``capabilities``: capacidades puntuales
    ``(capability, scope_type, scope_id)`` — el llamador decide cuáles cuentan (al escribir, las
    VIVAS: pendientes y activas, porque una pendiente surte efecto al aprobarse; al leer, las
    activas, que son las únicas que el lector carga). Acepta enums o strings.

    Las fuentes son dicts publicables (van a ``public_context`` del 409): dicen QUÉ choca con
    ``security_officer`` para que la UI lo señale sin adivinar.
    """
    globales = {_v(g) for g in globals_}
    if GlobalCapability.SECURITY_OFFICER.value not in globales:
        return {}

    owner: list[dict] = []
    if _v(base_role) == GatewayRole.OWNER.value:
        owner.append({"kind": "base_role", "role": GatewayRole.OWNER.value})
    for scope_type, scope_id, role in scope_roles:
        if _v(role) == GatewayRole.OWNER.value:
            owner.append(
                {
                    "kind": "scope_grant",
                    "scope_type": _v(scope_type),
                    "scope_id": int(scope_id),
                    "role": GatewayRole.OWNER.value,
                }
            )
    for capability, scope_type, scope_id in capabilities:
        if _v(capability) in _OWNER_ONLY_VALUES:
            owner.append(
                {
                    "kind": "capability_grant",
                    "capability": _v(capability),
                    "scope_type": _v(scope_type),
                    "scope_id": int(scope_id),
                }
            )

    out: dict[str, list[dict]] = {}
    if owner:
        out[SOD_RULE_OWNER] = owner
    if GlobalCapability.ACCESS_ADMIN.value in globales:
        out[SOD_RULE_ACCESS_ADMIN] = [
            {"kind": "global_capability", "global_capability": GlobalCapability.ACCESS_ADMIN.value}
        ]
    return out


def uncovered(found: dict[str, list[dict]], covered: Iterable[str]) -> dict[str, list[dict]]:
    """Las reglas violadas que NINGUNA excepción viva (``covered``: sus reglas) cubre."""
    cubiertas = set(covered or ())
    return {r: s for r, s in found.items() if r not in cubiertas}
