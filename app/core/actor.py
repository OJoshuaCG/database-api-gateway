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

from dataclasses import dataclass, field
from typing import Literal

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
    role: GatewayRole | None = None
    global_capabilities: frozenset[GlobalCapability] = field(default_factory=frozenset)
    token_id: str | None = None
    project_id: int | None = None
    #: Grants por alcance, TIPADOS: ``(scope_type, scope_id, role)``. La capa 2 los usa para
    #: resolver el destino, y ahí el tipo importa — el entorno 3 y el servidor 3 son distintos.
    scope_roles: frozenset[tuple[str, int, GatewayRole]] = field(default_factory=frozenset)

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
    """
    gr = list(grants or [])
    effective = union_role(role, {i: r for i, (_, _, r) in enumerate(gr)})
    caps = set(role_capabilities(effective))
    from app.services.capability_catalog import GLOBAL_CAPABILITIES

    for g in globals_:
        caps |= GLOBAL_CAPABILITIES[g]
    return Actor(
        kind="admin",
        id=user_id,
        username=username,
        capabilities=frozenset(caps),
        role=effective,
        global_capabilities=frozenset(globals_),
        scope_roles=frozenset(gr),
    )


def token_actor(
    *, token_pk: int, token_id: str, name: str, scopes: str, project_id: int
) -> Actor:
    """
    Actor de un token de API (servidor MCP).

    Las capacidades son la INTERSECCIÓN de los scopes declarados con el techo de agente. No es
    defensivo por gusto: una fila manipulada o legada nunca puede otorgar una capacidad fuera
    del techo, incluso si el string lo dice.
    """
    return Actor(
        kind="api_token",
        id=token_pk,
        username=name,
        capabilities=parse_scopes(scopes) & AGENT_ALLOWED,
        token_id=token_id,
        project_id=project_id,
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
