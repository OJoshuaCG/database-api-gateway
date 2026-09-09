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
    def _session(self):
        """
        Sesión ORM contra la BD de metadatos.

        Se construye por llamada y no en un ``__init__`` porque este controller lo instancian
        rutas que NO tocan la BD (``/authz/catalog`` es todo cálculo en memoria): abrir una
        conexión para servirlas sería pagarla en el 99 % de las llamadas.
        """
        from app.core.database import Database

        return Database().get_declarative_base_session()

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

    def scope_readiness(self) -> dict:
        """
        Qué pasaría si se empezara a otorgar acceso por alcance, HOY.

        Existe porque la capa 2 tiene un costo operativo que no se ve venir: una BD sin
        ``environment_id`` **no resuelve al entorno por defecto** —ése es el más permisivo— sino
        al más protegido. Así que el día que alguien reciba su primer grant restrictivo sobre
        producción, toda base sin clasificar queda tratada como producción para él.

        El plan lo pone como PRECONDICIÓN y no como una pantalla más: se clasifica primero, se
        otorga después. Este reporte es lo que dice cuánto falta.

        **Cero conexiones al motor**: se lee el inventario del gateway y nada más. Un reporte de
        preparación que dependa de que N motores respondan es un reporte que no se puede correr
        el día que hace falta.
        """
        from sqlalchemy import func

        from app.core.scope import most_protected_environment_id
        from app.models.environment import Environment
        from app.models.managed_database import ManagedDatabase
        from app.models.server import Server

        session = self._session()
        try:
            total = session.query(func.count(ManagedDatabase.id)).scalar() or 0
            sin_clasificar = (
                session.query(func.count(ManagedDatabase.id))
                .filter(ManagedDatabase.environment_id.is_(None))
                .scalar()
                or 0
            )

            por_servidor = []
            for srv in session.query(Server).order_by(Server.name).all():
                filas = (
                    session.query(ManagedDatabase.environment_id)
                    .filter(ManagedDatabase.server_id == srv.id)
                    .all()
                )
                ids = [f[0] for f in filas]
                faltantes = sum(1 for i in ids if i is None)
                # La MISMA regla que `resolve_environment_id`, no una segunda implementación:
                # sin bases o con al menos una sin clasificar, el servidor entero cae en el
                # más protegido. Si el reporte usara otro criterio, diría una cosa y el guard
                # haría otra — que es peor que no tener reporte.
                if not ids or faltantes:
                    derivado_id = most_protected_environment_id()
                else:
                    peor = (
                        session.query(Environment)
                        .filter(Environment.id.in_(ids))
                        .order_by(Environment.rank.desc(), Environment.id.desc())
                        .first()
                    )
                    derivado_id = peor.id if peor else None
                derivado = session.get(Environment, derivado_id) if derivado_id else None
                por_servidor.append(
                    {
                        "server_id": srv.id,
                        "server_name": srv.name,
                        "engine": str(srv.engine),
                        "databases": len(ids),
                        "unclassified": faltantes,
                        "derived_environment_slug": derivado.slug if derivado else None,
                        "derived_from_gap": bool(not ids or faltantes),
                    }
                )

            protegido = most_protected_environment_id()
            env = session.get(Environment, protegido) if protegido else None
            return {
                "total_databases": total,
                "unclassified_databases": sin_clasificar,
                "ready": sin_clasificar == 0,
                "fallback_environment_slug": env.slug if env else None,
                "servers": por_servidor,
            }
        finally:
            session.close()
