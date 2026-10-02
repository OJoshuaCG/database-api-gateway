"""
Capa 2 de autorización: ¿puede en ESTE destino?

LAS DOS CAPAS, Y POR QUÉ LA PRIMERA ES MÁS LAXA
-----------------------------------------------
La capa 1 (``require()``) pregunta *"¿podría, en algún alcance?"* usando el rol **UNIÓN** — el
máximo sobre el rol base y los grants. El máximo y no el mínimo es deliberado: con el mínimo,
"lector en producción" degradaría también el trabajo en desarrollo, que es justo el caso de uso
que motiva todo el eje de alcance.

La contrapartida es que la capa 1 es más laxa que la política real, y eso **solo** es aceptable
porque la capa 2 no es salteable. Esa no-saltabilidad la da el **keyword obligatorio sin
default**: ``assert_scope`` no se puede llamar sin declarar el destino, así que un sitio que se
olvide de resolverlo no pasa silenciosamente — falla con ``TypeError``. Es el mismo patrón que
``export_controller._validate_scope`` usa con ``target``.

EL GRANT NO SE SUMA AL ROL BASE: LO REEMPLAZA EN SU ALCANCE
-----------------------------------------------------------
Es la decisión que hace funcionar el caso de uso, y la que un ``max`` habría arruinado. Con
``base=operator`` y un grant ``viewer`` sobre producción, ``max(operator, viewer) = operator`` y
la restricción **no haría nada**. Así que:

- si hay grant para el alcance del destino, **el grant manda**;
- si no hay ninguno, manda el rol base;
- si hay DOS que aplican (uno por entorno y otro por servidor), manda el **más restrictivo**.

Lo último es fail-closed por elección: dos restricciones que se superponen no pueden resolverse a
la más permisiva, porque entonces agregar un grant podría *ampliar* el acceso sin que nadie lo
pida.

``NULL`` NO ES "PERMITIDO", Y ES EL COSTO OPERATIVO DE ESTO
-----------------------------------------------------------
Una BD sin ``environment_id`` **no resuelve al entorno por defecto**: el propio
``app/models/environment.py`` advierte que el default es *el más permisivo*, así que usarlo acá
convertiría el hueco de datos en un permiso. Resuelve al entorno de ``rank`` MÁXIMO, o sea el más
protegido.

Un destino a nivel SERVIDOR (los 21 endpoints que operan por referencia cruda, sin fila de
inventario) resuelve al máximo ``rank`` entre las bases de ese servidor, **tratando cualquier
``NULL`` como el máximo global**. Un servidor sin ninguna base clasificada cae entonces también
en el más protegido: es un caso particular de la misma regla, no una cláusula aparte.

> "Sin entorno derivable" **nunca** significa "permitido". Ese es el default que convierte un
> modelo de alcance en decoración.

**Y de ahí sale un costo real que hay que decir en voz alta**: el día que alguien reciba su primer
grant por alcance, toda base sin clasificar queda tratada como producción. Por eso existe el
reporte de ``GET /authz/scope-readiness``: se clasifica primero, se otorga después.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.core.actor import Actor
from app.core.capability_resolution import (
    capability_at,
    grants_allow,
    needs_target_resolution,
)
from app.exceptions import AppHttpException
from app.services.capability_catalog import (
    CODE_FORBIDDEN,
    Capability,
    GatewayRole,
    role_capabilities,
)

_ROLE_RANK = {GatewayRole.VIEWER: 0, GatewayRole.OPERATOR: 1, GatewayRole.OWNER: 2}


def _session():
    from app.core.database import Database

    return Database().get_declarative_base_session()


def most_protected_environment_id() -> int | None:
    """
    El entorno de ``rank`` máximo, con desempate por ``id`` — el orden total ``(rank, id)`` que
    define ``environment_sort_key``.

    Devuelve ``None`` solo si la tabla está vacía, que no pasa en un despliegue sembrado. Ese
    caso lo trata ``resolve_environment_id`` y **no** cae en "permitido".
    """
    from app.models.environment import Environment

    session = _session()
    try:
        fila = (
            session.query(Environment)
            .order_by(Environment.rank.desc(), Environment.id.desc())
            .first()
        )
        return fila.id if fila else None
    finally:
        session.close()


def resolve_environment_id(
    *, server_id: int | None, managed_database_id: int | None
) -> int | None:
    """
    El entorno del destino. Fail-closed: ver el docstring del módulo.

    Los dos parámetros son keyword y **sin default** a propósito: quien llame tiene que decir
    explícitamente que no tiene uno de los dos, en vez de omitirlo por descuido.
    """
    from app.models.managed_database import ManagedDatabase

    if managed_database_id is not None:
        session = _session()
        try:
            bd = session.get(ManagedDatabase, managed_database_id)
            if bd is None:
                # Un destino inexistente no puede resolver a un entorno permisivo. Que el 404
                # lo levante el controller es correcto; acá lo único que importa es no abrir.
                return most_protected_environment_id()
            if bd.environment_id is not None:
                return bd.environment_id
            return most_protected_environment_id()
        finally:
            session.close()

    if server_id is not None:
        from app.models.environment import Environment

        session = _session()
        try:
            filas = (
                session.query(ManagedDatabase.environment_id)
                .filter(ManagedDatabase.server_id == server_id)
                .all()
            )
            if not filas or any(f[0] is None for f in filas):
                # Sin bases, o con al menos una sin clasificar: el servidor entero se trata
                # como el entorno más protegido. Es la regla del §4.3 y su caso límite.
                return most_protected_environment_id()
            ids = [f[0] for f in filas]
            peor = (
                session.query(Environment)
                .filter(Environment.id.in_(ids))
                .order_by(Environment.rank.desc(), Environment.id.desc())
                .first()
            )
            return peor.id if peor else most_protected_environment_id()
        finally:
            session.close()

    # Ningún destino declarado: es una operación global. No hay entorno que resolver y el rol
    # base es la respuesta — no se inventa el más protegido, porque eso rompería toda operación
    # de catálogo para quien tenga un grant restrictivo en producción.
    return None


@dataclass(frozen=True, slots=True)
class ScopeTarget:
    """
    Destino DECLARADO de una ruta: qué es, no dónde cae. Sin BD y sin autenticación.

    Lo produce un resolvedor puro (``app.core.scope_targets``) que FastAPI ejecuta ANTES que la
    dependencia padre; por eso no puede tocar la BD: una consulta ahí sería un oráculo de
    existencia previo a la autenticación y al CSRF. La resolución a entornos la hace
    ``resolve_points`` y solo después de autenticar.

    ``quantifier``: ``all`` exige la capacidad en CADA punto (jobs de doble extremo, listas
    explícitas, operaciones de blueprint); ``any`` exige al menos UN punto permitido (lotes
    implícitos: la dependencia prueba que hay algo que hacer y el controller particiona).
    """

    kind: str  # vocabulario cerrado: las claves de ``scope_targets._RESOLVERS``
    params: tuple = ()
    quantifier: Literal["all", "any"] = "all"


@dataclass(frozen=True, slots=True)
class ScopePoint:
    """Un punto concreto donde se evalúa la capacidad. ``environment_id`` ya es fail-closed."""

    environment_id: int | None
    server_id: int | None
    item_id: int | None = None  # id de la BD gestionada o del ítem del lote, para particionar


@dataclass(frozen=True, slots=True)
class ScopePartition:
    permitted: tuple[int, ...]
    forbidden: tuple[int, ...]


def resolve_points(target: ScopeTarget) -> list[ScopePoint]:
    """
    Resuelve un destino declarado a sus puntos de evaluación (1-2 consultas, sin N+1).

    Una lista vacía solo es posible para blueprints sin ninguna BD (``model``): ahí no hay
    escritura remota y la decisión cae al rol base. Todo otro tipo, ante un destino inexistente
    o irresoluble, devuelve el entorno más protegido.
    """
    from app.core.scope_targets import resolve_target_points

    return resolve_target_points(target)


def role_at_point(actor: Actor, point: ScopePoint) -> GatewayRole:
    """
    El rol del actor EN ESTE punto. Función pura: no toca la BD.

    Es ``effective_role_at`` sin la resolución del entorno, extraída para evaluar N puntos ya
    resueltos. Sin grant aplicable manda el rol BASE (nunca la unión); con dos aplicables, el
    más restrictivo. Ver el docstring del módulo.
    """
    if actor.role is None:
        # Token: su alcance por destino es el `project_id`, otra frontera.
        return GatewayRole.VIEWER

    base = actor.base_role if actor.base_role is not None else actor.role
    aplicables = [
        role
        for (scope_type, scope_id, role) in actor.scope_roles
        if (
            scope_type == "environment"
            and point.environment_id is not None
            and scope_id == point.environment_id
        )
        or (
            scope_type == "server"
            and point.server_id is not None
            and scope_id == point.server_id
        )
    ]
    if not aplicables:
        return base
    return min(aplicables, key=lambda r: _ROLE_RANK[r])


def effective_role_at(
    actor: Actor, *, server_id: int | None, managed_database_id: int | None
) -> GatewayRole:
    """
    El rol del actor EN ESTE destino. Ver el docstring del módulo para la regla completa.

    Camino rápido: un actor sin ningún grant por alcance —el caso de todo despliegue hasta que
    alguien otorgue el primero— devuelve su rol base **sin tocar la BD**. Importa porque esto
    corre en el camino de cada operación con destino.

    Sin grant aplicable manda el rol BASE (``actor.base_role``), NUNCA ``actor.role``: ese es
    el rol UNIÓN, que ya incluye el máximo de todos los grants. Caer a la unión hacía que un
    grant que ELEVA (``viewer`` con ``owner`` en desarrollo) valiera en todos los destinos,
    incluido producción — exactamente lo contrario de "el grant manda en SU alcance". Los
    tests previos no lo veían porque todos usaban base ≥ grant fuera del alcance del grant.
    """
    if actor.role is None:
        return GatewayRole.VIEWER

    base = actor.base_role if actor.base_role is not None else actor.role
    if not actor.scope_roles:
        return base

    env_id = resolve_environment_id(
        server_id=server_id, managed_database_id=managed_database_id
    )
    return role_at_point(actor, ScopePoint(environment_id=env_id, server_id=server_id))


def _forbidden(actor: "Actor | dict | None", capability: Capability) -> AppHttpException:
    """
    El 403 único de las dos capas. No dice cuál negó: distinguir "no tenés la capacidad" de "no
    la tenés acá" le regala a un atacante el mapa de sus propios alcances por fuerza bruta.

    ``actor`` y ``capability`` son para el RASTRO (``app.core.denial_audit``, agregado y
    best-effort), nunca para la respuesta: construir el 403 ya deja la fila, así que ningún
    camino de denegación de esta capa queda sin rastro.
    """
    from app.core.denial_audit import record_denial

    record_denial(CODE_FORBIDDEN, actor=actor, capability=capability, check="scope")
    return AppHttpException(
        message="No tienes permiso para esta operación.",
        status_code=403,
        public_context={"code": CODE_FORBIDDEN},
    )


def _permits(
    actor: Actor,
    role: GatewayRole,
    capability: Capability,
    point: ScopePoint | None = None,
) -> bool:
    """
    ¿``role`` más las globales y las capacidades puntuales EN ``point`` conceden ``capability``?

    Las capacidades globales (`access_admin`, `security_officer`) no tienen alcance: son
    ortogonales a la cadena de roles, así que lo que otorgan no se recorta por destino. Las
    capacidades puntuales SUMAN al rol y solo valen si su alcance coincide con el ``point``; sin
    ``point`` (destino global) no coincide ninguna.
    """
    from app.services.capability_catalog import GLOBAL_CAPABILITIES

    permitidas = set(role_capabilities(role))
    for g in actor.global_capabilities:
        permitidas |= GLOBAL_CAPABILITIES[g]
    if capability in permitidas:
        return True
    if point is None:
        return False
    return grants_allow(
        actor,
        capability,
        environment_id=point.environment_id,
        server_id=point.server_id,
    )


def assert_at(actor: Actor, capability: Capability, target: ScopeTarget) -> None:
    """
    Capa 1 + capa 2 sobre un destino declarado. 403 ``access.forbidden`` idéntico en ambas.

    Es lo que usan los escalamientos por payload de una ruta con ``require_at``: dentro de una
    ruta con alcance declarado, ``assert_capability`` (solo capa 1, rol UNIÓN) sería un hueco
    con forma de chequeo — el check 6b del script lo prohíbe.

    Sin grants por alcance, o con un actor de token, la capa 2 es un no-op y NO toca la BD.
    """
    if not actor.has(capability):
        raise _forbidden(actor, capability)
    assert_layer2(actor, capability, target)


def assert_at_point(actor: Actor, capability: Capability, point: ScopePoint) -> None:
    """
    Capa 1 + capa 2 sobre un punto YA resuelto. Para los controllers que conocen el entorno
    recién después de resolverlo (alta o adopción sin ``environment_id`` explícito).

    Sin grants por alcance, o con un actor de token, no toca la BD. Mismo 403 en ambas capas.
    """
    if not actor.has(capability):
        raise _forbidden(actor, capability)
    if not needs_target_resolution(actor, capability):
        return
    if not _permits(actor, role_at_point(actor, point), capability, point):
        raise _forbidden(actor, capability)


def assert_layer2(actor: Actor, capability: Capability, target: ScopeTarget) -> None:
    """Solo la capa 2. ``require_at`` la usa tras la capa 1 que ya corrió en ``_authenticate``."""
    if not needs_target_resolution(actor, capability):
        return

    puntos = resolve_points(target)
    if not puntos:
        # Blueprint sin BDs: no hay escritura remota, decide el rol base.
        base = actor.base_role if actor.base_role is not None else actor.role
        if not _permits(actor, base, capability):
            raise _forbidden(actor, capability)
        return

    if target.quantifier == "any":
        # Un ítem con varios puntos (origen y destino de una clonación) se permite solo si TODOS
        # sus puntos se permiten; basta con un ítem permitido para pasar la capa 1 del lote.
        ok = any(_item_verdicts(actor, capability, puntos).values())
    else:
        ok = all(
            _permits(actor, role_at_point(actor, p), capability, p) for p in puntos
        )
    if not ok:
        raise _forbidden(actor, capability)


def _item_verdicts(
    actor: Actor, capability: Capability, points: Sequence[ScopePoint]
) -> dict:
    """
    ``item_id`` → permitido. Un ítem con varios puntos (los dos extremos de una clonación) exige
    TODOS. Un punto sin ``item_id`` es un ítem propio. Los tokens y los actores sin grants por
    alcance lo ven todo permitido: la capa 2 no les aplica.
    """
    sin_capa2 = not needs_target_resolution(actor, capability)
    veredictos: dict = {}
    for indice, p in enumerate(points):
        clave = p.item_id if p.item_id is not None else ("sin_id", indice)
        ok = sin_capa2 or _permits(actor, role_at_point(actor, p), capability, p)
        veredictos[clave] = veredictos.get(clave, True) and ok
    return veredictos


def partition_by_scope(
    *, actor: Actor, capability: Capability, points: Sequence[ScopePoint]
) -> ScopePartition:
    """
    Parte los ítems de un lote en permitidos y prohibidos, por ``item_id``.

    ``actor`` es keyword-only y sin default: un lote que se olvide de pasarlo falla con
    ``TypeError`` en vez de particionar con nadie. Los tokens y los actores sin grants por
    alcance lo ven todo permitido (la capa 2 no les aplica). Un ítem con varios puntos (mismo
    ``item_id``) queda prohibido si CUALQUIERA de ellos lo está.
    """
    for p in points:
        if p.item_id is None:
            raise ValueError("partition_by_scope exige item_id en cada punto")
    veredictos = _item_verdicts(actor, capability, points)
    return ScopePartition(
        tuple(k for k, ok in veredictos.items() if ok),
        tuple(k for k, ok in veredictos.items() if not ok),
    )


def partition_for_batch(
    *,
    admin: "dict | Actor | None",
    capability: Capability,
    points_fn: Callable[[], Sequence[ScopePoint]],
    explicit: bool,
) -> ScopePartition | None:
    """
    La regla por ítem de un lote, en un solo lugar para que los cuatro lotes no diverjan.

    - ``None``: la capa 2 no aplica (token, actor sin grants por alcance, o un llamador interno
      sin ``Actor``). El llamador no filtra nada y ``points_fn`` ni se evalúa: cero BD.
    - ``explicit`` (el cliente nombró los ítems) con algún prohibido → 403 y no corre nada.
    - Ítems implícitos: los prohibidos se devuelven para OMITIRLOS; si no queda ninguno
      permitido → 403. El 403 es el mismo ``access.forbidden`` de las dos capas.
    """
    if not isinstance(admin, Actor) or not needs_target_resolution(admin, capability):
        return None
    puntos = list(points_fn())
    particion = partition_by_scope(actor=admin, capability=capability, points=puntos)
    if (explicit and particion.forbidden) or (puntos and not particion.permitted):
        raise _forbidden(admin, capability)
    return particion


def assert_scope(
    actor: Actor,
    capability: Capability,
    *,
    server_id: int | None,
    managed_database_id: int | None,
) -> None:
    """
    Exige la capacidad EN ESTE destino. Levanta 403 con el mismo código cerrado que la capa 1.

    Los dos ``keyword`` son **obligatorios y sin default**, y eso es lo que hace que la capa 2
    no sea salteable: un sitio que se olvide de declarar el destino falla con ``TypeError`` en
    la suite, no en silencio. Pasar ``None`` explícito es una declaración —"esto es global"—, no
    un olvido.

    El 403 **no dice cuál de las dos capas negó**: distinguir "no tenés la capacidad" de "no la
    tenés acá" le regala a un atacante el mapa de sus propios alcances por fuerza bruta.
    """
    if not capability_at(
        actor,
        capability,
        server_id=server_id,
        managed_database_id=managed_database_id,
    ):
        raise _forbidden(actor, capability)


def assert_scope_for_database(actor: Actor, capability: Capability, *, db_id: int) -> None:
    """
    Atajo para el destino más común: una BD del inventario.

    Resuelve también el ``server_id`` de esa BD, porque un grant con alcance de SERVIDOR tiene
    que aplicar a las operaciones sobre sus bases. Sin eso, "lector en el servidor de
    producción" no diría nada sobre las bases de ese servidor, que es lo único que hay ahí.
    """
    from app.models.managed_database import ManagedDatabase

    session = _session()
    try:
        bd = session.get(ManagedDatabase, db_id)
        server_id = bd.server_id if bd else None
    finally:
        session.close()

    assert_scope(
        actor, capability, server_id=server_id, managed_database_id=db_id
    )
