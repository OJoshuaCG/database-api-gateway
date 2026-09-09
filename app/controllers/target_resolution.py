"""
Qué bases alcanza un agente. El gate de política, en UN solo lugar.

Existe aparte de ``common.py`` —que queda intacto— porque lo que resuelve no es "traeme el
servidor": es *"¿a qué base tiene derecho a llegar ESTE actor"*. Son dos preguntas distintas, y
mezclarlas es cómo un camino nuevo termina saltándose el gate.

LO QUE ESTE MÓDULO **NO** TIENE TODAVÍA, Y POR QUÉ
--------------------------------------------------
No resuelve un destino de CONEXIÓN. Eso exige la credencial de solo lectura del servidor
(``servers.readonly_*``) y el façade que la arma, y las dos cosas llegan con las tools que sí
leen el catálogo del motor. Ponerlas acá antes de tener quién las use dejaría un resolvedor que
niega siempre y tres columnas sin escritor — la regla de "cero flags inertes" aplicada al revés.

La tool de la v1 que ya funciona (``list_databases``) **no toca ningún motor**: lee el inventario
del gateway. Así que lo que hace falta es el filtro de alcance, y es lo que hay acá.

EL ORDEN ES AUTORIZACIÓN PRIMERO, POLÍTICA DESPUÉS
--------------------------------------------------
Y no de más barato a más caro. Ver el docstring de ``app/services/mcp_catalog.py``: los ejes son
todos consultas locales y la diferencia de costo es ruido, mientras que un orden por costo
convierte los códigos de política en un **oráculo de inventario**.

Para un LISTADO el orden se vuelve otra cosa: no hay un objeto que negar, así que la política se
aplica como **filtro** y lo que el agente recibe es la lista de lo que sí alcanza. Un listado que
enumerara lo negado sería el mismo oráculo por otra vía.
"""

from dataclasses import dataclass

from app.core.actor import Actor
from app.exceptions import AppHttpException
from app.services import mcp_catalog as codes
from app.services.capability_catalog import Capability


@dataclass(frozen=True, slots=True)
class ReachableDatabase:
    """
    Una base que el agente sí alcanza.

    ``database`` es SIEMPRE el nombre de la fila de inventario. Nunca se propaga un string que
    haya mandado el agente: si se propagara, el gate validaría una fila y la operación siguiente
    podría apuntar a otra.
    """

    database_id: int
    database: str
    server_id: int
    engine: str
    environment_slug: str | None
    model_id: int | None
    model_slug: str | None
    model_version: str | None


def assert_agent_scope(actor: Actor) -> None:
    """
    Eje 2 del gate: el scope del token.

    Se evalúa **antes de tocar la BD** porque no hace falta ninguna lectura para saber que el
    token no tiene la capacidad — y hacer la lectura primero le daría al agente una señal de
    tiempo sobre si la base existe.
    """
    if not actor.has(Capability.BLUEPRINTS_READ):
        raise AppHttpException(
            message="El token no tiene el scope necesario.",
            status_code=403,
            public_context={"code": codes.CODE_SCOPE_DENIED},
        )


def reachable_databases(actor: Actor) -> list[ReachableDatabase]:
    """
    Las bases que el agente alcanza: proyecto del token **Y** entorno permite **Y** base con
    opt-in **Y** base no vetada.

    Los cuatro ejes son un `AND` en la misma consulta a propósito. Escritos como filtros
    sucesivos en Python, cada uno sería un lugar donde alguien puede agregar un `or` — y acá el
    modo de fallo de un `or` mal puesto es entregarle la estructura de la base de un tercero a
    un agente.

    **El opt-in por base es el eje que decide el alcance**, no el veto: con solo el veto,
    habilitar un entorno abriría de golpe todas sus bases, incluidas las que nadie revisó y las
    que se creen después.
    """
    from app.core.database import Database
    from app.models.database_model import DatabaseModel
    from app.models.environment import Environment
    from app.models.managed_database import ManagedDatabase
    from app.models.project import ProjectDatabaseModel
    from app.models.server import Server

    assert_agent_scope(actor)

    session = Database().get_declarative_base_session()
    try:
        filas = (
            session.query(ManagedDatabase, Server, DatabaseModel, Environment)
            .join(Server, Server.id == ManagedDatabase.server_id)
            .join(DatabaseModel, DatabaseModel.id == ManagedDatabase.model_id)
            .join(Environment, Environment.id == ManagedDatabase.environment_id)
            .join(
                ProjectDatabaseModel,
                ProjectDatabaseModel.model_id == ManagedDatabase.model_id,
            )
            .filter(
                # Eje 3 — el proyecto del token. Es un JOIN y no un filtro posterior: así una
                # base de otro proyecto simplemente no está, en vez de estar y ser negada.
                ProjectDatabaseModel.project_id == actor.project_id,
                # Ejes 6, 7 y 8.
                Environment.allows_agent_access.is_(True),
                ManagedDatabase.agent_access_allowed.is_(True),
                ManagedDatabase.agent_access_blocked.is_(False),
            )
            .order_by(ManagedDatabase.name)
            .all()
        )
        # El eje 5 (base sin entorno) no necesita filtro propio: el `join` con `Environment` ya
        # excluye las que tienen `environment_id` nulo. Se declara porque su ausencia parece un
        # olvido y no lo es.
        return [
            ReachableDatabase(
                database_id=bd.id,
                database=bd.name,
                server_id=srv.id,
                engine=str(srv.engine),
                environment_slug=env.slug,
                model_id=bd.model_id,
                model_slug=modelo.slug if modelo else None,
                model_version=bd.model_version,
            )
            for (bd, srv, modelo, env) in filas
        ]
    finally:
        session.close()
