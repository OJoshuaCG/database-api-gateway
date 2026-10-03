"""
``Actor`` — quién está haciendo la request.

Es el punto de convergencia de las DOS autenticaciones del gateway: la sesión del
administrador (cookie firmada) y, cuando exista, el token de API del servidor MCP. Las dos
resuelven al mismo tipo, así que hay **una sola comprobación de autorización y un solo
vocabulario** — no dos políticas que divergen en silencio, que es el riesgo que el plan 11 §9
dejó anotado.

POR QUÉ ES UN DATACLASS FROZEN CON SLOTS, Y NO UN DICT
------------------------------------------------------
Hoy circula un ``dict`` (``{"id": …, "username": …}``) por **227 sitios** de controllers y
rutas. Migrarlos es mecánico, pero un olvido silencioso sería caro: con un dict, un
``actor.get("id")`` mal escrito devuelve ``None`` y la fila de ``audit_log`` queda con
``admin_id`` nulo — un agujero de auditabilidad que nadie ve.

``slots=True`` sin ``.get()`` y sin ``__getitem__`` convierte cada sitio olvidado en un
``AttributeError``/``TypeError`` **ruidoso**. Por eso **no hay shim de compatibilidad**: el
valor entero del tipo es que el olvido falle fuerte y temprano.

**Y esa propiedad tiene una excepción que costó un fallo real:** un sitio olvidado que corre
dentro de un ``try/except`` best-effort **no** es ruidoso — el ``AttributeError`` se lo traga el
except y el efecto es justo el silencioso que el tipo existía para evitar. Pasó con
``_record_history`` de la consola SQL, cuyo swallow es correcto para su propio propósito (una
fila de historial no debe tirar abajo una operación ya ejecutada en el motor), así que el
problema no era el except: era leer la identidad a mano. **Toda lectura de identidad va por
``identity_of``.**

``frozen=True`` además impide que un camino de código "corrija" el actor a mitad de request,
que es la clase de bug donde "quién es el actor" y "qué política aplica" divergen.

EL ROL NUNCA VIENE DE LA COOKIE
-------------------------------
``role`` y ``capabilities`` se computan al autenticar, leyendo la BD — igual que ya se hace con
``is_active``. La cookie de sesión lleva solo el id.

No es por confidencialidad (la cookie está firmada): es que **un rol en la cookie es un rol que
no se puede revocar**. El ``SessionMiddleware`` de Starlette re-firma la cookie en cada
respuesta, así que ``SESSION_MAX_AGE`` es un timeout de INACTIVIDAD y una sesión activa no
expira nunca. Degradar a alguien no surtiría efecto jamás.

Ver ``docs/plans/13-usuarios-y-autorizacion-del-gateway.md`` §6.2 y §7.1.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.core.capability_resolution import CapabilityGrantKey, layer1_capabilities
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    Capability,
    GatewayRole,
    GlobalCapability,
    parse_scopes,
    role_capabilities,
    union_role,
)

ActorKind = Literal["admin", "api_token"]


@dataclass(frozen=True, slots=True)
class Actor:
    """
    Identidad resuelta de una request. Inmutable y sin acceso por clave, a propósito.

    ``capabilities`` es el conjunto EFECTIVO y es lo único que ``require()`` consulta: una
    comprobación, un vocabulario. ``role`` no es un segundo eje — es la *entrada* al conjunto
    para humanos, igual que ``scopes`` lo es para máquinas.
    """

    kind: ActorKind
    id: int
    username: str
    capabilities: frozenset[Capability]
    #: Rol UNIÓN (máximo sobre el base y los grants): es la entrada de ``capabilities`` para la
    #: capa 1. NO sirve como respuesta de la capa 2 cuando ningún grant aplica al destino; para
    #: eso está ``base_role``.
    role: GatewayRole | None = None
    #: El ``gateway_role`` del usuario ANTES de la unión con los grants. Es lo que manda en un
    #: destino donde ningún grant aplica (``scope.effective_role_at``). ``None`` en tokens.
    base_role: GatewayRole | None = None
    global_capabilities: frozenset[GlobalCapability] = field(default_factory=frozenset)
    token_id: str | None = None
    project_id: int | None = None
    #: Grants por alcance, TIPADOS: ``(scope_type, scope_id, role)``. La capa 2 los usa para
    #: resolver el destino, y ahí el tipo importa — el entorno 3 y el servidor 3 son distintos.
    scope_roles: frozenset[tuple[str, int, GatewayRole]] = field(default_factory=frozenset)
    #: Capacidades puntuales ACTIVAS: ``(capacidad, scope_type, scope_id)``. SUMAN al rol (nunca
    #: restan); ``capabilities`` ya incluye su ``expand`` para la capa 1 y la capa 2 las empareja
    #: con el destino (``capability_resolution``). Siempre vacío en tokens.
    capability_grants: frozenset[CapabilityGrantKey] = field(default_factory=frozenset)
    #: Fin de la ventana de step-up de la SESIÓN (UTC naive, como ``gateway_sessions``), o
    #: ``None`` si no hay ninguna abierta. Solo actores ``admin`` resueltos desde una sesión; un
    #: actor armado fuera de una sesión no tiene ventana y el step-up le falla cerrado. Ver
    #: ``app/core/step_up.py``.
    step_up_until: datetime | None = None
    #: Solo en tokens: el ``Actor`` del usuario que EMITIÓ el token, tal como está AHORA (se relee
    #: por request, no se congela al emitir). Es la entrada del techo del token: ver
    #: ``token_actor``. ``None`` en actores ``admin``.
    issuer: "Actor | None" = None

    def has(self, capability: Capability) -> bool:
        """La única pregunta que hace el gate de capacidad."""
        return capability in self.capabilities

    @property
    def is_agent(self) -> bool:
        return self.kind == "api_token"


def admin_actor(
    *,
    user_id: int,
    username: str,
    role: GatewayRole,
    grants: "list[tuple[str, int, GatewayRole]] | None" = None,
    globals_: frozenset[GlobalCapability] = frozenset(),
    capability_grants: "Iterable[CapabilityGrantKey] | None" = None,
    step_up_until: datetime | None = None,
) -> Actor:
    """
    Actor de un administrador humano.

    ``capabilities`` sale del rol UNIÓN (el máximo sobre el rol base y los grants por alcance)
    más lo que aporten las capacidades globales. La unión responde "¿podría, en algún
    alcance?"; el alcance concreto lo decide la capa 2, en el resolvedor de destino.

    ``grants`` llega TIPADO —``(scope_type, scope_id, role)``— y no como un dict por id. La
    versión anterior perdía el ``scope_type`` y con eso un grant de servidor se leía como uno
    de entorno: el entorno 3 y el servidor 3 son cosas distintas. Para la unión da igual
    (solo mira los roles), pero la capa 2 resuelve por destino y ahí el tipo ES la pregunta.

    ``base_role`` guarda el ``role`` recibido SIN la unión. Antes solo se guardaba la unión, y
    la capa 2 caía a ella cuando ningún grant aplicaba: un ``viewer`` con ``owner`` en
    desarrollo resolvía ``owner`` también en producción y pasaba ``assert_scope(DROP)``. Un
    grant que ELEVA tiene que valer solo dentro de su alcance.
    """
    gr = list(grants or [])
    effective = union_role(role, {i: r for i, (_, _, r) in enumerate(gr)})
    caps = set(role_capabilities(effective))
    from app.services.capability_catalog import GLOBAL_CAPABILITIES

    for g in globals_:
        caps |= GLOBAL_CAPABILITIES[g]
    # Las capacidades puntuales SUMAN (con su lectura implícita) en la capa 1; la capa 2 decide
    # en qué destino valen. Ver ``capability_resolution``.
    puntuales = frozenset(capability_grants or ())
    caps = set(layer1_capabilities(caps, puntuales))
    return Actor(
        kind="admin",
        id=user_id,
        username=username,
        capabilities=frozenset(caps),
        role=effective,
        base_role=role,
        global_capabilities=frozenset(globals_),
        scope_roles=frozenset(gr),
        capability_grants=puntuales,
        step_up_until=step_up_until,
    )


def token_actor(
    *,
    token_pk: int,
    token_id: str,
    name: str,
    scopes: str,
    project_id: int,
    issuer: Actor | None = None,
) -> Actor:
    """
    Actor de un token de API (servidor MCP).

    Las capacidades son la INTERSECCIÓN de tres cosas: los scopes declarados, el techo de agente
    y lo que el EMISOR puede hoy en la capa 1 (``issuer.capabilities``, que ya incluye rol unión,
    capacidades globales y capacidades puntuales). Un token es una delegación acotada de quien lo
    emitió: nunca puede ganar algo que su emisor no tiene, y si al emisor le quitan una
    capacidad el token la pierde en la siguiente request.

    Fail-closed en el lector: sin ``issuer`` las capacidades son vacías. ``authenticate_agent``
    rechaza antes a los tokens sin emisor válido; esto es la segunda barrera para quien arme un
    actor por otro camino.

    Alcance: solo CAPACIDADES. No restringe el alcance por entorno/destino, porque el modelo de
    roles no tiene denegación por entorno (un ``viewer`` lee todo); lo que acota el destino de un
    token es su ``project_id``.
    """
    heredables = issuer.capabilities if issuer is not None else frozenset()
    return Actor(
        kind="api_token",
        id=token_pk,
        username=name,
        capabilities=parse_scopes(scopes) & AGENT_ALLOWED & heredables,
        token_id=token_id,
        project_id=project_id,
        issuer=issuer,
    )


def identity_of(subject: "Actor | dict | None") -> tuple[int | None, str | None]:
    """
    ``(id, username)`` de una identidad, sea un ``Actor`` o el ``dict`` legado.

    Es el ÚNICO lugar donde se lee la identidad de un sujeto que puede tener las dos formas.
    Vive acá y no en ``audit`` porque no es solo para auditar: la persistencia del historial de
    la consola SQL, la autoría de un lote y el ``_guard_owner`` de exportación leen lo mismo — y
    ese último es una decisión de AUTORIZACIÓN, donde un ``None`` silencioso abre la puerta en
    vez de cerrarla.

    Normalizar acá es lo que permite migrar de a un módulo **sin** darle un ``.get()`` al
    ``Actor``: el tipo sigue estricto (ver el docstring del módulo), y los sitios que sí tienen
    que leer identidad la piden por nombre.

    **Se simplifica —no se retira— cuando no queden rutas con el guard legado** (lo mide
    ``scripts/check_route_capabilities.py``): ahí la rama del ``dict`` se cae y queda la lectura
    de atributos.
    """
    if subject is None:
        return None, None
    if isinstance(subject, dict):
        return subject.get("id"), subject.get("username")
    return getattr(subject, "id", None), getattr(subject, "username", None)


def actor_type_of(subject: "Actor | dict | None") -> str:
    """
    La CLASE de actor (``"admin"`` | ``"api_token"``) de una identidad, para persistirla.

    Sale del propio actor y no de un parámetro que el llamador pueda equivocar. Un ``dict``
    legado o ``None`` son siempre ``"admin"``, que es la verdad histórica: antes de los tokens
    de agente no había otra clase de actor. Mismo vocabulario que ``audit_log.actor_type``.

    Vive junto a ``identity_of`` por el mismo motivo: la auditoría, el historial de
    aplicación y la autoría de una versión de blueprint leen lo mismo, y tres copias de la
    regla divergen en silencio el día que aparezca una tercera clase de actor.
    """
    return getattr(subject, "kind", None) or "admin"
