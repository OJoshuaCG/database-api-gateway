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

from app.core.actor import Actor
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


def effective_role_at(
    actor: Actor, *, server_id: int | None, managed_database_id: int | None
) -> GatewayRole:
    """
    El rol del actor EN ESTE destino. Ver el docstring del módulo para la regla completa.

    Camino rápido: un actor sin ningún grant por alcance —el caso de todo despliegue hasta que
    alguien otorgue el primero— devuelve su rol base **sin tocar la BD**. Importa porque esto
    corre en el camino de cada operación con destino.
    """
    if actor.role is None:
        # Un actor de tipo token no tiene rol: sus capacidades salen de los scopes, ya
        # intersectados con el techo de agente. El alcance por destino de un token es el
        # `project_id`, que es otra frontera y no ésta.
        return GatewayRole.VIEWER

    if not actor.scope_roles:
        return actor.role

    env_id = resolve_environment_id(
        server_id=server_id, managed_database_id=managed_database_id
    )

    aplicables = [
        role
        for (scope_type, scope_id, role) in actor.scope_roles
        if (scope_type == "environment" and env_id is not None and scope_id == env_id)
        or (scope_type == "server" and server_id is not None and scope_id == server_id)
    ]
    if not aplicables:
        return actor.role
    return min(aplicables, key=lambda r: _ROLE_RANK[r])


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
    rol = effective_role_at(
        actor, server_id=server_id, managed_database_id=managed_database_id
    )
    # Las capacidades globales (`access_admin`, `security_officer`) no tienen alcance: son
    # ortogonales a la cadena de roles, así que lo que otorgan no se recorta por destino.
    from app.services.capability_catalog import GLOBAL_CAPABILITIES

    permitidas = set(role_capabilities(rol))
    for g in actor.global_capabilities:
        permitidas |= GLOBAL_CAPABILITIES[g]

    if capability not in permitidas:
        raise AppHttpException(
            message="No tienes permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_FORBIDDEN},
        )


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
