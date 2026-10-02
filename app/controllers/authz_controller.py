"""
Controller de la autorización: ``/auth/me`` y ``/authz/catalog``.

No toca ningún motor: es todo plano de control. Vive como controller y no en la ruta porque el
cálculo de ``capabilities`` tiene que salir del MISMO predicado que hace cumplir ``require()``,
y eso es lógica, no serialización.
"""

import hashlib
import json
import logging

from app.core.actor import Actor
from app.models.user_model import UserModel
from app.services.capability_catalog import Capability, capability_matrix, spec

logger = logging.getLogger(__name__)


def _sweep_expired_grants() -> None:
    """
    Vencimiento perezoso (D6) sin poder tumbar la lectura que lo dispara.

    Es mantenimiento, no autorización: si falla —la tabla todavía no existe porque el código
    llegó antes que la migración, o se hizo un downgrade— se registra y se sigue, igual que el
    barrido de arranque en ``main.py``. El loader de la autorización ya ignora las vencidas.
    """
    from app.controllers.capability_grant_controller import CapabilityGrantController

    try:
        CapabilityGrantController().expire_overdue()
    except Exception:
        logger.exception("Vencimiento perezoso de capacidades puntuales falló; se sigue.")


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
        propias = self._own_live_grants(actor)
        return {
            "id": actor.id,
            "username": actor.username,
            # ``role`` conserva su significado (rol UNIÓN: "¿podría en algún alcance?") por
            # compatibilidad con la SPA; ``base_role`` es el que rige donde ningún grant aplica,
            # y es lo que la SPA necesita para decidir por destino con ``scope_roles``.
            "role": actor.role.value if actor.role else None,
            "base_role": actor.base_role.value if actor.base_role else None,
            "capabilities": sorted(c.value for c in effective),
            "global_capabilities": sorted(g.value for g in actor.global_capabilities),
            "scope_roles": [
                {"scope_type": st, "scope_id": sid, "role": role.value}
                for st, sid, role in sorted(actor.scope_roles, key=lambda t: (t[0], t[1]))
            ],
            "step_up_capabilities": sorted(
                c.value for c in effective if spec(c).requires_step_up
            ),
            "capability_grants": propias,
            "previous_login_at": fila.get("previous_login_at"),
            "last_failed_at": fila.get("last_failed_at"),
            "catalog_version": _catalog_version(),
        }

    @staticmethod
    def _own_live_grants(actor: Actor) -> list[dict]:
        """
        Capacidades puntuales VIVAS de quien pregunta, y solo de él (``actor.id``, nunca un
        parámetro). Un token de agente no tiene ninguna. Vence perezosamente antes de leer (D6)
        para no mostrar como pendiente una solicitud ya vencida.
        """
        if actor.kind != "admin":
            return []
        from app.models.capability_grant_model import CapabilityGrantModel

        _sweep_expired_grants()
        model = CapabilityGrantModel()
        try:
            filas = model.list_live_for_user(actor.id)
            nombres = model.scope_names([(f["scope_type"], f["scope_id"]) for f in filas])
        except Exception:
            # /auth/me es lo que arranca la SPA: sin la tabla se publica la lista vacía (que es
            # lo que el loader de la autorización aplica en ese caso) en vez de un 500.
            logger.exception("No se pudieron leer las capacidades puntuales de la sesión.")
            return []
        return [
            {
                "id": f["id"],
                "capability": f["capability"],
                "scope_type": f["scope_type"],
                "scope_id": f["scope_id"],
                "scope_name": nombres.get((f["scope_type"], f["scope_id"])),
                "status": f["status"],
                "expires_at": f["expires_at"],
            }
            for f in filas
        ]

    def effective_access(self, user_id: int, actor: Actor) -> dict:
        """
        Acceso efectivo de ``user_id`` con procedencia, para el ``access_admin``.

        Ni una línea de lógica propia: el contexto sale de ``find_access_context`` (el de la
        autorización real), el ``Actor`` de ``actor_from_access_context`` y las filas de
        ``explain``. Un test exige que el conjunto de capacidades no inertes sea igual a
        ``Actor.capabilities``. Solo ``access_admin`` (ni siquiera ``security_officer``, y nadie
        lee el de otro por esta vía: la propia persona usa ``/auth/me``).
        """
        from app.controllers.capability_grant_controller import assert_access_admin
        from app.controllers.gateway_user_controller import CODE_NOT_FOUND
        from app.core.authz import actor_from_access_context
        from app.core.capability_resolution import explain
        from app.exceptions import AppHttpException
        from app.models.capability_grant_model import CapabilityGrantModel

        assert_access_admin(actor)
        usuario = UserModel().find_by_id(user_id)
        if not usuario:
            raise AppHttpException(
                message="Usuario del gateway no encontrado.",
                status_code=404,
                public_context={"code": CODE_NOT_FOUND},
                context={"user_id": user_id},
            )
        _sweep_expired_grants()
        activo = bool(usuario.get("is_active"))
        ctx = UserModel().find_access_context(user_id, include_inert=True)
        modelo = actor_from_access_context(user_id, usuario["username"], ctx)
        entradas = explain(ctx, active=activo)
        nombres = CapabilityGrantModel().scope_names(
            [(t, i) for t, i, _ in modelo.scope_roles]
            + [(e.scope_type, e.scope_id) for e in entradas if e.scope_type and e.scope_id]
        )

        def nombre(t, i):
            return nombres.get((t, i)) if t and i else None

        return {
            "user_id": user_id,
            "username": usuario["username"],
            "active": activo,
            "base_role": modelo.base_role.value if modelo.base_role else None,
            "scope_roles": [
                {"scope_type": t, "scope_id": i, "scope_name": nombre(t, i), "role": r.value}
                for t, i, r in sorted(modelo.scope_roles, key=lambda x: (x[0], x[1]))
            ],
            "global_capabilities": sorted(g.value for g in modelo.global_capabilities),
            "capabilities": [
                {
                    "capability": e.capability.value,
                    "source": e.source,
                    "scope_type": e.scope_type,
                    "scope_id": e.scope_id,
                    "scope_name": nombre(e.scope_type, e.scope_id),
                    "grant_id": e.grant_id,
                    "implied_by": e.implied_by.value if e.implied_by else None,
                    "inert": e.inert,
                }
                for e in entradas
            ],
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

        **Limitación F-17**: la resolución a nivel servidor mira solo las BDs INVENTARIADAS; una BD
        del motor fuera del inventario no cuenta para el entorno más protegido. Se informa en
        ``server_resolution_inventory_only`` (siempre true) en vez de listar el motor.

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
                # F-17: documentado y expuesto, no resuelto. Listar el motor al autorizar rompería
                # la regla de cero conexiones; el reporte avisa que el hueco existe.
                "server_resolution_inventory_only": True,
            }
        finally:
            session.close()
