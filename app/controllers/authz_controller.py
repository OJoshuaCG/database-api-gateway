"""
Controller de la autorización: ``/auth/me`` y ``/authz/catalog``.

No toca ningún motor: es todo plano de control. Vive como controller y no en la ruta porque el
cálculo de ``capabilities`` tiene que salir del MISMO predicado que hace cumplir ``require()``,
y eso es lógica, no serialización.
"""

import hashlib
import json

from app.core.actor import Actor
from app.models.user_model import UserModel
from app.services.capability_catalog import Capability, capability_matrix, spec


def _catalog_version() -> str:
    """
    Huella del catálogo publicado, para que el cliente sepa cuándo invalidar su caché.

    Se calcula sobre la matriz SERIALIZADA y ordenada, no sobre la lista de ids: así cambia
    también cuando cambia qué rol tiene qué capacidad, que es justo lo que a la UI le importa.
    """
    payload = json.dumps(capability_matrix(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class AuthzController:
    def me(self, actor: Actor) -> dict:
        """
        Identidad y capacidades efectivas del actor.

        ``capabilities`` se deriva iterando el enum con el MISMO ``actor.has()`` que consulta
        ``require()``. No hay una segunda lista que mantener sincronizada: es un predicado, dos
        consumidores.
        """
        effective = [c for c in Capability if actor.has(c)]
        # Una consulta extra sobre `users`, y solo acá: la traza de autenticación NO vive en el
        # `Actor`. Ponerla ahí obligaría a leerla en CADA request para servirla en uno, y el
        # `Actor` es identidad y capacidades — dos timestamps de diagnóstico no son ninguna de
        # las dos. `/auth/me` lo llama la SPA al cargar, no por request.
        fila = UserModel().find_by_id(actor.id) or {}
        return {
            "id": actor.id,
            "username": actor.username,
            "role": actor.role.value if actor.role else None,
            "capabilities": sorted(c.value for c in effective),
            "global_capabilities": sorted(g.value for g in actor.global_capabilities),
            "scope_roles": [
                {"scope_type": st, "scope_id": sid, "role": role.value}
                for st, sid, role in sorted(actor.scope_roles, key=lambda t: (t[0], t[1]))
            ],
            "step_up_capabilities": sorted(
                c.value for c in effective if spec(c).requires_step_up
            ),
            "previous_login_at": fila.get("previous_login_at"),
            "last_failed_at": fila.get("last_failed_at"),
            "catalog_version": _catalog_version(),
        }

    def catalog(self) -> list[dict]:
        """El catálogo completo, para que la SPA renderice etiquetas sin hardcodear vocabulario."""
        return capability_matrix()
