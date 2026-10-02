"""
Partir un cambio de acceso en lo que se aplica YA y lo que pide un segundo aprobador (C3). Puro.

POR QUÉ EXISTE
--------------
El techo por TENENCIA ("nunca otorgás más de lo que tenés") obligaba a que quien administra
accesos tuviera también cada deber que reparte: para crear un ``owner`` había que ser ``owner``,
para crear un ``security_officer`` había que serlo. Es exactamente la combinación que la
separación de deberes vino a deshacer, así que el techo se retiró. Lo que él impedía —un
administrador solo se crea un títere ``owner``, recibe su invitación y entra con esa cara— lo
impide ahora el SEGUNDO APROBADOR: toda elevación (``needs_second_approver``) queda pendiente
hasta que OTRO ``access_admin`` la apruebe.

LA PARTICIÓN
------------
``split(actual, deseado)`` devuelve el estado INMEDIATO (lo que se aplica en el request) y la
lista de elevaciones. El inmediato es el deseado SIN lo que eleva:

- rol base: ``owner`` desde algo que no era ``owner`` queda en el rol actual;
- globales: las que se AGREGAN quedan pendientes; las que se quitan, se quitan ya;
- alcances: un ``owner`` nuevo en un alcance deja ese alcance como estaba (o sin grant si no
  había); el resto (altas ``viewer``/``operator``, bajas, cambios a menos) se aplica ya.

**Las bajas se aplican siempre en el acto**: retirar acceso no puede esperar a una segunda
persona. Y el inmediato nunca agrega una fuente de conflicto de separación de deberes (no suma
``owner`` ni globales), así que aplicarlo no puede crear una violación nueva.

``state_hash`` es la huella del acceso sobre la que se pidió la elevación: si al aprobar no
coincide, el acceso cambió entretanto y la solicitud está vieja (``access.request_stale``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.services.capability_catalog import GatewayRole, needs_second_approver

_OWNER = GatewayRole.OWNER.value


@dataclass(frozen=True)
class AccessState:
    """Rol base, globales y alcances ``(scope_type, scope_id, role)`` de una cuenta."""

    base_role: str
    globals_: frozenset[str]
    grants: frozenset[tuple[str, int, str]]

    @classmethod
    def of(cls, base_role, globals_, grants) -> "AccessState":
        return cls(
            base_role=str(getattr(base_role, "value", base_role) or GatewayRole.VIEWER.value),
            globals_=frozenset(str(getattr(g, "value", g)) for g in (globals_ or ())),
            grants=frozenset(
                (str(t), int(i), str(getattr(r, "value", r))) for (t, i, r) in (grants or ())
            ),
        )

    def as_dict(self) -> dict:
        return {
            "gateway_role": self.base_role,
            "global_capabilities": sorted(self.globals_),
            "scope_grants": [
                {"scope_type": t, "scope_id": i, "role": r} for (t, i, r) in sorted(self.grants)
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AccessState":
        return cls.of(
            d.get("gateway_role"),
            d.get("global_capabilities") or [],
            [(g["scope_type"], g["scope_id"], g["role"]) for g in d.get("scope_grants") or []],
        )


def state_hash(state: AccessState) -> str:
    """SHA-256 de la forma canónica del acceso. Mismo acceso ⇒ misma huella, sin importar el orden."""
    canon = json.dumps(state.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def split(actual: AccessState, deseado: AccessState) -> tuple[AccessState, list[dict]]:
    """
    ``(inmediato, elevaciones)``. ``elevaciones`` son dicts publicables (van en la respuesta
    ``202`` y en la bandeja) con la misma forma ``kind`` que las fuentes de ``access.sod_conflict``.
    Vacía ⇒ el cambio entero se aplica ya.
    """
    elevaciones: list[dict] = []

    base = deseado.base_role
    if base != actual.base_role and needs_second_approver(role=base) and actual.base_role != _OWNER:
        elevaciones.append({"kind": "base_role", "role": base})
        base = actual.base_role

    nuevas = sorted(deseado.globals_ - actual.globals_)
    for g in nuevas:
        if needs_second_approver(global_capability=g):
            elevaciones.append({"kind": "global_capability", "global_capability": g})
    globales = (actual.globals_ & deseado.globals_) | {
        g for g in nuevas if not needs_second_approver(global_capability=g)
    }

    por_alcance_actual = {(t, i): r for (t, i, r) in actual.grants}
    grants: set[tuple[str, int, str]] = set()
    for t, i, r in sorted(deseado.grants):
        previo = por_alcance_actual.get((t, i))
        if needs_second_approver(role=r) and previo != r:
            elevaciones.append({"kind": "scope_grant", "scope_type": t, "scope_id": i, "role": r})
            if previo is not None:
                grants.add((t, i, previo))
            continue
        grants.add((t, i, r))

    inmediato = AccessState(base_role=base, globals_=frozenset(globales), grants=frozenset(grants))
    return inmediato, elevaciones
