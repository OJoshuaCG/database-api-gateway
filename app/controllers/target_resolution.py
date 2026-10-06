"""
Qué bases alcanza un agente. El gate de política, en UN solo lugar.

Existe aparte de ``common.py`` —que queda intacto— porque lo que resuelve no es "traeme el
servidor": es *"¿a qué base tiene derecho a llegar ESTE actor"*. Son dos preguntas distintas, y
mezclarlas es cómo un camino nuevo termina saltándose el gate.

DOS PREGUNTAS, DOS FUNCIONES
----------------------------
``reachable_databases`` es el LISTADO: la política se aplica como filtro y el agente recibe lo que
sí alcanza. ``resolve_agent_database`` es UNA base pedida por id: ahí sí hay un objeto que negar,
y la negación sigue el orden del plan 12 §5.2 (autorización primero, política después).
``open_readonly`` encadena la segunda con la credencial de SOLO LECTURA del servidor y el façade:
es el único camino por el que una tool llega a un motor, y **nunca** lee la pseudo-root.

EL ALCANCE DEL TOKEN ES POR PROYECTO, Y EL PIVOTE ES N:M
--------------------------------------------------------
Acá hubo una fuga real, encontrada en auditoría y reproducida: ``managed_databases`` **no tiene
``project_id``**, así que la única vía para saber qué proyecto alcanza una base es el pivote
``project_database_models`` — y ese pivote es N:M **por diseño**, porque el propio
``app/models/project.py`` declara que "una BD compartida entre iniciativas es el caso normal".

Consecuencia: con un blueprint vinculado a los proyectos A y B, un token acotado a A veía las
bases de B —nombre, entorno y versión aplicada— porque comparten el blueprint. Es reconocimiento
cross-tenant, y el operador de B había puesto el opt-in creyendo que habilitaba a *su* agente.

**El arreglo es fail-closed y no una heurística: una base solo es alcanzable si su blueprint
pertenece a EXACTAMENTE UN proyecto, y ese proyecto es el del token.** Un blueprint compartido
deja la base fuera del alcance de todos los agentes, incluido el del proyecto que "debería"
verla. Es más restrictivo que lo que alguien podría querer, y es lo correcto mientras el modelo
no tenga forma de expresar *para qué proyecto* se abrió una base — eso pide una columna y va
anotado como follow-up, no resuelto a ojo acá.

EL ORDEN ES AUTORIZACIÓN PRIMERO, POLÍTICA DESPUÉS
--------------------------------------------------
Y no de más barato a más caro. Ver el docstring de ``app/services/mcp_catalog.py``: los ejes son
todos consultas locales y la diferencia de costo es ruido, mientras que un orden por costo
convierte los códigos de política en un **oráculo de inventario**.

Para un LISTADO el orden se vuelve otra cosa: no hay un objeto que negar, así que la política se
aplica como **filtro** y lo que el agente recibe es la lista de lo que sí alcanza. Un listado que
enumerara lo negado sería el mismo oráculo por otra vía.
"""

from contextlib import contextmanager
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


def assert_agent_scope(actor: Actor, capability: Capability) -> None:
    """
    Eje 2 del gate: el scope del token, contra la capacidad que exige la tool que llama.

    Se evalúa **antes de tocar la BD** porque no hace falta ninguna lectura para saber que el
    token no tiene la capacidad — y hacer la lectura primero le daría al agente una señal de
    tiempo sobre si la base existe.

    El dispatcher ya hace el mismo chequeo con ``ToolSpec.scope`` (``3289038``). Se repite acá
    porque este módulo también lo llaman caminos que no pasan por el dispatcher, y la capacidad
    llega por parámetro y no hardcodeada: antes era siempre ``blueprints.read``, así que una tool
    con otro scope quedaba protegida solo por el dispatcher.
    """
    if not actor.has(capability):
        raise AppHttpException(
            message="El token no tiene el scope necesario.",
            status_code=403,
            public_context={"code": codes.CODE_SCOPE_DENIED},
        )


def reachable_databases(actor: Actor, capability: Capability) -> list[ReachableDatabase]:
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
    from sqlalchemy import func

    from app.core.database import Database
    from app.core.environments import MCP_MAX_OBJECTS
    from app.models.database_model import DatabaseModel
    from app.models.environment import Environment
    from app.models.managed_database import ManagedDatabase
    from app.models.project import ProjectDatabaseModel
    from app.models.server import Server

    assert_agent_scope(actor, capability)

    session = Database().get_declarative_base_session()
    try:
        # Blueprints que pertenecen a EXACTAMENTE un proyecto. Es la mitad del arreglo de la
        # fuga cross-proyecto: sin esto, un blueprint compartido entre dos proyectos hace que el
        # token de uno alcance las bases del otro.
        exclusivos = (
            session.query(ProjectDatabaseModel.model_id)
            .group_by(ProjectDatabaseModel.model_id)
            .having(func.count(func.distinct(ProjectDatabaseModel.project_id)) == 1)
            .subquery()
        )
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
                # …y su blueprint no puede estar compartido con otro proyecto. Las dos
                # condiciones juntas son el alcance real; la primera sola es la fuga.
                ManagedDatabase.model_id.in_(session.query(exclusivos.c.model_id)),
                # Ejes 6, 7 y 8.
                Environment.allows_agent_access.is_(True),
                ManagedDatabase.agent_access_allowed.is_(True),
                ManagedDatabase.agent_access_blocked.is_(False),
            )
            .order_by(ManagedDatabase.name)
            # Tope de objetos ANTES de materializar. El presupuesto de bytes del dispatch corre
            # después de serializar, así que sin esto un proyecto con miles de bases hace que el
            # proceso construya N×4 objetos ORM para después descartarlos — 636 ms de CPU por
            # cada 120 bytes de request, medido. Se pide uno más que el tope para poder
            # distinguir "justo el tope" de "se pasó".
            .limit(MCP_MAX_OBJECTS + 1)
            .all()
        )
        if len(filas) > MCP_MAX_OBJECTS:
            raise AppHttpException(
                message=(
                    f"El proyecto tiene más de {MCP_MAX_OBJECTS} bases alcanzables. Acotá el "
                    "alcance del token a un proyecto más chico: no se trunca a propósito, "
                    "porque una lista cortada le haría creer al agente que no hay más."
                ),
                status_code=413,
                public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
            )
        # El eje 5 (base sin entorno) no necesita filtro propio: el `join` con `Environment` ya
        # excluye las que tienen `environment_id` nulo. Se declara porque su ausencia parece un
        # olvido y no lo es.
        return [
            ReachableDatabase(
                database_id=bd.id,
                database=bd.name,
                server_id=srv.id,
                # `.value` y no `str()`: `EngineType` es `str, Enum` y `Enum.__str__` gana, así
                # que `str()` devolvía "EngineType.mysql" — y eso es lo que veía el agente.
                engine=srv.engine.value if hasattr(srv.engine, "value") else str(srv.engine),
                environment_slug=env.slug,
                model_id=bd.model_id,
                model_slug=modelo.slug if modelo else None,
                model_version=bd.model_version,
            )
            for (bd, srv, modelo, env) in filas
        ]
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Una base pedida por id: el gate completo, en el orden del plan 12 §5.2         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgentDatabase:
    """
    Una base que el agente pidió por id y que pasó el gate completo.

    No lleva la credencial: ``open_readonly`` la lee recién al abrir la sesión, así que nada de lo
    que una tool reciba acá contiene un secreto.
    """

    database: ReachableDatabase
    quarantined: bool
    #: Bandera del servidor ``readonly_proc_grant`` (``SELECT ON mysql.proc`` para la credencial
    #: de solo lectura). Decide si un cuerpo de rutina ausente se explica con ``flag_off``. El
    #: default ``False`` mantiene a los constructores existentes y a un servidor sin la columna.
    readonly_proc_grant: bool = False


def _deny(code: str, status: int, message: str) -> AppHttpException:
    return AppHttpException(message=message, status_code=status, public_context={"code": code})


def resolve_agent_database(
    actor: Actor, database_id: int, capability: Capability
) -> AgentDatabase:
    """
    El gate de UNA base, eje por eje y en el orden del plan 12 §5.2.

    1. ``MCP_ENABLED`` — lo corta ``authenticate_agent`` antes de llegar acá.
    2. Scope del token — ``assert_agent_scope``, sin leer nada.
    3. **Pertenencia al proyecto** — en la MISMA consulta que trae la fila, con el mismo criterio
       que el listado (blueprint exclusivo del proyecto). Una base de otro proyecto y una que no
       existe responden igual, ``mcp.not_found``, y con el mismo número de consultas.
    4. Credencial de solo lectura **registrada y verificada hace menos de**
       ``MCP_READONLY_MAX_AGE_DAYS`` — fail-closed y sin fallback a pseudo-root, con ningún flag.
    5–8. Política: sin entorno, entorno que no admite agentes, sin opt-in, vetada.

    Los ejes 4 a 8 SÍ se distinguen porque a esta altura la base es del proyecto del token: decir
    "existe y no te la puedo dar" evita reintentos y no le enseña nada que no sepa. Para lo que
    está fuera del proyecto, la respuesta es "no encontrado", nunca "denegado".
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from app.core.database import Database
    from app.core.environments import MCP_READONLY_MAX_AGE_DAYS
    from app.models.database_model import DatabaseModel
    from app.models.enums import ProvisionStatus
    from app.models.environment import Environment
    from app.models.managed_database import ManagedDatabase
    from app.models.project import ProjectDatabaseModel
    from app.models.server import Server

    assert_agent_scope(actor, capability)
    no_encontrada = _deny(codes.CODE_NOT_FOUND, 404, "La base no existe o no es de tu proyecto.")
    if not isinstance(database_id, int) or isinstance(database_id, bool) or database_id < 1:
        raise no_encontrada

    session = Database().get_declarative_base_session()
    try:
        exclusivos = (
            session.query(ProjectDatabaseModel.model_id)
            .group_by(ProjectDatabaseModel.model_id)
            .having(func.count(func.distinct(ProjectDatabaseModel.project_id)) == 1)
            .subquery()
        )
        fila = (
            session.query(ManagedDatabase, Server, DatabaseModel, Environment)
            .join(Server, Server.id == ManagedDatabase.server_id)
            .join(DatabaseModel, DatabaseModel.id == ManagedDatabase.model_id)
            .join(
                ProjectDatabaseModel,
                ProjectDatabaseModel.model_id == ManagedDatabase.model_id,
            )
            # OUTER join: una base sin entorno tiene que llegar hasta el eje 5 y negarse ahí con
            # su código, no desaparecer como si fuera de otro proyecto.
            .outerjoin(Environment, Environment.id == ManagedDatabase.environment_id)
            .filter(
                ManagedDatabase.id == database_id,
                ProjectDatabaseModel.project_id == actor.project_id,
                ManagedDatabase.model_id.in_(session.query(exclusivos.c.model_id)),
            )
            .first()
        )
        if fila is None:
            raise no_encontrada
        bd, srv, modelo, env = fila

        limite = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            days=MCP_READONLY_MAX_AGE_DAYS
        )
        if not (
            srv.readonly_username
            and srv.readonly_password_encrypted
            and srv.readonly_verified_at is not None
            and srv.readonly_verified_at > limite
        ):
            raise _deny(
                codes.CODE_READONLY_MISSING,
                403,
                (
                    "El servidor de esta base no tiene una credencial de solo lectura verificada "
                    f"en los últimos {MCP_READONLY_MAX_AGE_DAYS} días. Un agente nunca lee un "
                    "motor con la credencial de administración."
                ),
            )
        if env is None:
            raise _deny(
                codes.CODE_ENV_UNASSIGNED, 403, codes.POLICY_HINTS[codes.CODE_ENV_UNASSIGNED]
            )
        if not env.allows_agent_access:
            raise _deny(codes.CODE_ENV_DENIES, 403, codes.POLICY_HINTS[codes.CODE_ENV_DENIES])
        if not bd.agent_access_allowed:
            raise _deny(
                codes.CODE_NOT_OPTED_IN, 403, codes.POLICY_HINTS[codes.CODE_NOT_OPTED_IN]
            )
        if bd.agent_access_blocked:
            raise _deny(codes.CODE_BLOCKED, 403, codes.POLICY_HINTS[codes.CODE_BLOCKED])

        return AgentDatabase(
            database=ReachableDatabase(
                database_id=bd.id,
                database=bd.name,
                server_id=srv.id,
                engine=srv.engine.value if hasattr(srv.engine, "value") else str(srv.engine),
                environment_slug=env.slug,
                model_id=bd.model_id,
                model_slug=modelo.slug if modelo else None,
                model_version=bd.model_version,
            ),
            quarantined=bd.status == ProvisionStatus.error,
            readonly_proc_grant=bool(srv.readonly_proc_grant),
        )
    finally:
        session.close()


def resolve_agent_data_database(
    actor: Actor, database_id: int, capability: Capability
) -> AgentDatabase:
    """
    El gate de DATOS de UNA base: todo ``resolve_agent_database`` MÁS lo propio de leer filas.

    Orden, y cada eje se evalúa SIEMPRE después del anterior (autorización antes que política,
    como en el gate de estructura):

    1. ``resolve_agent_database`` — scope, proyecto, credencial de estructura, entorno, opt-in de
       estructura y veto. Una base ajena o inexistente responde ``mcp.not_found``.
    2. **Kill switch** de la capacidad (``MCP_DATA_READ_ENABLED`` / ``MCP_DATA_QUERY_ENABLED``),
       leído en CADA llamada: apagarlo corta aunque el token y la base estén abiertos.
    3. Credencial de datos de ESA base registrada (``ManagedDatabaseDataCredential``).
    4. Sonda verde con ``verified_at`` más nuevo que ``MCP_DATA_CREDENTIAL_MAX_AGE_DAYS``.
    5. Opt-in de datos abierto (``data_access_allowed``) y con aprobador registrado: una fila con
       el flag en 1 pero sin ``data_access_approved_by_id`` (un ``UPDATE`` a mano) no abre nada.

    Los códigos son los internos de ``mcp_catalog`` (``mcp.data_*``): salen al agente traducidos
    a ``DATA_DISABLED`` / ``PROBE_NOT_GREEN``. No descifra nada ni abre conexión: el secreto lo
    lee recién quien ejecuta.
    """
    from app.core.database import Database
    from app.models.managed_database_data_credential import ManagedDatabaseDataCredential
    from app.services.capability_catalog import data_capability_enabled

    resuelta = resolve_agent_database(actor, database_id, capability)
    if not data_capability_enabled(capability):
        raise _deny(
            codes.CODE_DATA_DISABLED,
            403,
            "Las tools de datos están apagadas en este gateway (kill switch).",
        )

    session = Database().get_declarative_base_session()
    try:
        cred = (
            session.query(ManagedDatabaseDataCredential)
            .filter(
                ManagedDatabaseDataCredential.managed_database_id
                == resuelta.database.database_id
            )
            .first()
        )
        _assert_data_credential_open(cred)
    finally:
        session.close()
    return resuelta


def _assert_data_credential_open(cred) -> None:
    """
    La credencial de datos de una base tiene que existir, tener una sonda VERDE y fresca
    (``data_probe_is_fresh`` con ``MCP_DATA_CREDENTIAL_MAX_AGE_DAYS``) y el opt-in aprobado.

    Es lo que mantiene a las tools de datos atadas a una OBSERVACIÓN reciente del motor y no a la
    promesa del provisionado: antes de esto nada enforzaba la frescura al LEER. Una fecha futura
    (reloj corrido) cuenta como no fresca. Lo comparten el gate y ``_data_target`` (que vuelve a
    exigirlo justo antes de descifrar), así que no hay dos criterios.
    """
    from datetime import UTC, datetime

    from app.core.environments import MCP_DATA_CREDENTIAL_MAX_AGE_DAYS
    from app.services.db_admin.readonly_probe import data_probe_is_fresh

    if cred is None or not cred.username or not cred.password_encrypted:
        raise _deny(
            codes.CODE_DATA_CREDENTIAL_MISSING,
            403,
            "La base no tiene credencial de datos registrada.",
        )
    if not data_probe_is_fresh(
        cred.verified_at,
        now=datetime.now(UTC).replace(tzinfo=None),
        max_age_days=MCP_DATA_CREDENTIAL_MAX_AGE_DAYS,
    ):
        raise _deny(
            codes.CODE_DATA_PROBE_STALE,
            403,
            (
                "La credencial de datos no tiene una sonda verde de los últimos "
                f"{MCP_DATA_CREDENTIAL_MAX_AGE_DAYS} días."
            ),
        )
    if not cred.data_access_allowed or cred.data_access_approved_by_id is None:
        raise _deny(
            codes.CODE_DATA_NOT_OPTED_IN,
            403,
            "La base no tiene el opt-in de lectura de datos aprobado.",
        )


def _readonly_target(server_id: int):
    """
    El ``ServerTarget`` con la credencial de SOLO LECTURA. Es la ÚNICA función de este módulo que
    descifra algo, y **no lee ``root_password_encrypted``**: no hay rama por la que la
    pseudo-root llegue a una tool, ni siquiera por error.

    Se vuelve a exigir la credencial completa aunque ``resolve_agent_database`` ya la validó: entre
    las dos lecturas alguien pudo quitarla, y la consecuencia de no chequear es un agente
    hablándole a un motor con lo que haya quedado en la fila.
    """
    from app.core.crypto import CryptoConfigError, CryptoError, decrypt
    from app.core.database import Database
    from app.core.environments import REMOTE_SSL_MODE
    from app.core.remote_engine import ServerTarget
    from app.models.server import Server

    session = Database().get_declarative_base_session()
    try:
        srv = session.get(Server, server_id)
        if srv is None or not (srv.readonly_username and srv.readonly_password_encrypted):
            raise _deny(
                codes.CODE_READONLY_MISSING,
                403,
                "El servidor de esta base no tiene una credencial de solo lectura registrada.",
            )
        try:
            password = decrypt(srv.readonly_password_encrypted)
        except (CryptoError, CryptoConfigError) as exc:
            raise _deny(
                codes.CODE_READONLY_MISSING,
                403,
                "La credencial de solo lectura del servidor no se pudo descifrar.",
            ) from exc
        return ServerTarget(
            server_id=srv.id,
            dialect=srv.engine.value if hasattr(srv.engine, "value") else str(srv.engine),
            host=srv.host,
            port=srv.port,
            admin_user=srv.readonly_username,
            admin_password=password,
            ssl_mode=srv.ssl_mode if srv.ssl_mode is not None else REMOTE_SSL_MODE,
        )
    finally:
        session.close()


@contextmanager
def open_readonly(actor: Actor, database_id: int, capability: Capability):
    """
    Gate completo + credencial de solo lectura + sesión de lectura. Rinde
    ``(AgentDatabase, ReadonlyIntrospector)`` y cierra la sesión SIEMPRE.

    El nombre de la base que se abre es el de la FILA de inventario, nunca un string del agente.
    Un timeout de la sesión se traduce a un error de tool con código propio, para que el agente
    no lo confunda con un fallo del servidor y reintente en loop.
    """
    from app.services.db_admin.export_session import ExportDurationExceeded
    from app.services.db_admin.readonly_introspector import readonly_introspection

    resuelta = resolve_agent_database(actor, database_id, capability)
    target = _readonly_target(resuelta.database.server_id)
    try:
        with readonly_introspection(target, resuelta.database.database) as facade:
            yield resuelta, facade
    except ExportDurationExceeded as exc:
        raise _deny(
            codes.CODE_SESSION_TIMEOUT,
            504,
            "La lectura del catálogo superó el tiempo máximo de la sesión del MCP. Pedí menos "
            "objetos por llamada.",
        ) from exc


# --------------------------------------------------------------------------- #
# Código de objetos (get_definition) y disponibilidad de cuerpos en list_objects  #
# --------------------------------------------------------------------------- #

_DEFINITION_AUDIT_ACTION = "mcp.get_definition"
#: Tope de la lista ``kind:nombre`` de la auditoría de intención. Los nombres salen del índice del
#: motor (no del agente), pero un esquema con nombres largos no puede inflar una fila de auditoría.
_DEFINITION_AUDIT_DETAIL_MAX_BYTES = 2048
_KINDS_WITH_BODY = ("view", "trigger", "event", "routine")


@dataclass(frozen=True, slots=True)
class DefinitionBatch:
    """
    Lo que ``read_definitions`` entrega al handler, ya sin sesión abierta: el handler mapea esto a
    la salida pública sin tocar nunca el façade ni la credencial.

    ``results`` son ``definition_reader.DefinitionResult`` (cuerpo ya redactado y medido).
    ``missing`` son los ``(kind, name, routine_kind)`` pedidos que NO están en el índice.
    """

    database: AgentDatabase
    results: tuple
    missing: tuple[tuple[str, str, str | None], ...]
    consistent_structure: bool
    session_warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BodyAvailability:
    """
    Disponibilidad del cuerpo por tipo de objeto para ESTE llamador (``list_objects``).

    ``reasons[kind]`` es ``None`` si el cuerpo se puede pedir con ``get_definition`` o la razón
    cerrada si no. ``routines_may_be_hidden`` avisa que el índice puede no listar rutinas.
    """

    reasons: dict
    routines_may_be_hidden: bool


def assert_definitions_enabled() -> None:
    """
    Kill switch de ``get_definition`` (``MCP_SCHEMA_DEFINITIONS_ENABLED``), leído en CADA llamada.

    Es el PRIMER paso de la tool y no lee ni el inventario: apagado, la respuesta no se distingue
    por tiempo ni por efectos de "la base no existe". Es un 403 con código propio y NO una razón
    por objeto: no es que un objeto no tenga cuerpo, es que la función entera está apagada.
    """
    from app.services.capability_catalog import data_capability_enabled

    if not data_capability_enabled(Capability.DATA_DEFINITIONS):
        raise _deny(
            codes.CODE_DEFINITIONS_DISABLED,
            403,
            "La lectura de definiciones está apagada en este gateway (kill switch).",
        )


def _partition_by_index(
    requested: list[tuple[str, str, str | None]], index: dict[str, list[str]]
) -> tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]:
    """
    ``(presentes, ausentes)`` contra el ÍNDICE de la base. Es la barrera contra la inyección y el
    cruce de bases: un nombre que no es exactamente uno del índice (``x`; DROP TABLE t --``,
    ``otra_base.v1``) nunca llega a un ``SHOW CREATE``; se informa en ``missing`` y listo.
    """
    names_by_kind = {kind: set(names) for kind, names in index.items()}
    present: list[tuple[str, str, str | None]] = []
    missing: list[tuple[str, str, str | None]] = []
    for kind, name, routine_kind in requested:
        if name in names_by_kind.get(kind, set()):
            present.append((kind, name, routine_kind))
        else:
            missing.append((kind, name, routine_kind))
    return present, missing


def _record_definition_intent(
    actor: Actor, resuelta: AgentDatabase, present: list[tuple[str, str, str | None]]
) -> None:
    """
    Intención de auditoría ANTES de leer el primer cuerpo. FAIL-CLOSED: si no se puede escribir, no
    se lee nada y el agente recibe ``AUDIT_UNAVAILABLE``.

    Guarda ``tipo:nombre`` de lo que se va a leer y NUNCA un cuerpo. Solo van los nombres que
    existen en el índice: los pedidos ausentes son texto del agente y no tienen por qué entrar a
    una fila de auditoría.
    """
    from app.core.logger import get_logger
    from app.services import audit
    from app.services.db_admin.agent_query import clean_text

    object_list = ",".join(f"{kind}:{name}" for kind, name, _ in present)
    detail = f"token={getattr(actor, 'token_id', None)} objects={clean_text(object_list)}"
    detail = detail.encode("utf-8")[:_DEFINITION_AUDIT_DETAIL_MAX_BYTES].decode(
        "utf-8", errors="ignore"
    )
    try:
        audit.record_intent(
            _DEFINITION_AUDIT_ACTION,
            admin=actor,
            target_type="managed_database",
            target_id=resuelta.database.database_id,
            server_id=resuelta.database.server_id,
            touched_engine=True,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed: sin auditoría no se lee ningún cuerpo
        get_logger(__name__).error("Auditoría de intención caída; no se leen definiciones")
        raise _deny(
            codes.REASON_AUDIT_UNAVAILABLE,
            503,
            "No se pudo registrar la auditoría; no se leyó ninguna definición.",
        ) from exc


def _record_definition_result(
    actor: Actor, resuelta: AgentDatabase, *, ok: bool, results: list, missing_count: int
) -> None:
    """
    Fila de resultado con CONTEOS por razón, sin cuerpos ni nombres. ``audit.record`` nunca lanza:
    la intención ya dejó el rastro fail-closed, esta fila completa el cuadro.
    """
    from collections import Counter

    from app.services import audit

    reason_counts = Counter(r.unavailable_reason for r in results if not r.body_available)
    available = sum(1 for r in results if r.body_available)
    redacted = sum(sum(r.redactions.values()) for r in results)
    reasons_text = " ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
    detail = (
        f"token={getattr(actor, 'token_id', None)} available={available} "
        f"missing={missing_count} redacted={redacted} {reasons_text}"
    ).strip()
    audit.record(
        _DEFINITION_AUDIT_ACTION,
        status="success" if ok else "failure",
        admin=actor,
        target_type="managed_database",
        target_id=resuelta.database.database_id,
        server_id=resuelta.database.server_id,
        touched_engine=True,
        detail=detail,
    )


def read_definitions(
    actor: Actor,
    database_id: int,
    objects: list[tuple[str, str, str | None]],
    capability: Capability,
) -> DefinitionBatch:
    """
    ``get_definition``: el código de hasta ``MAX_DEFINITIONS_PER_CALL`` objetos de UNA base.

    Orden (cada paso corta el siguiente):

    1. **Kill switch** (``assert_definitions_enabled``): antes de leer nada.
    2. Argumentos: el handler ya los validó sin conectar; acá solo queda la defensa del tope.
    3. ``open_readonly``: scope -> proyecto (``mcp.not_found``) -> credencial de ESTRUCTURA fresca
       -> entorno, opt-in y veto -> sesión READ ONLY.
    4. **Índice**: lo pedido que no está va a ``missing``. Es metadatos, no lee cuerpos.
    5. ``record_intent`` FAIL-CLOSED con ``tipo:nombre`` (jamás un cuerpo).
    6. Lectura por objeto + ``build_definition`` (redacta, mide, huella).
    7. Fila de resultado con conteos por razón.

    NO pasa por ``_data_gate``: los cuerpos se leen con la credencial de ESTRUCTURA (la misma de
    ``list_objects``), no con la de datos por base, y exigir esa le pondría a un token de
    definiciones un opt-in de datos que no corresponde. El control de acceso es el scope
    ``data.definitions`` más el kill switch. Los imports son perezosos por el guard de
    ``tests/test_mcp_import_guard.py``.
    """
    from app.services.db_admin.definition_reader import MAX_DEFINITIONS_PER_CALL, build_definition
    from app.services.db_admin.readonly_probe import routine_body_reason

    assert_definitions_enabled()
    if not objects or len(objects) > MAX_DEFINITIONS_PER_CALL:
        raise _deny(
            codes.CODE_INVALID_ARGUMENT,
            422,
            f"'objects' admite de 1 a {MAX_DEFINITIONS_PER_CALL} objetos por llamada.",
        )

    results: list = []
    with open_readonly(actor, database_id, capability) as (resuelta, facade):
        present, missing = _partition_by_index(objects, facade.object_index())
        if present:
            _record_definition_intent(actor, resuelta, present)
            try:
                server_version = facade.server_version()
                engine = resuelta.database.engine
                proc_flag = resuelta.readonly_proc_grant
                for kind, name, routine_kind in present:
                    reads = facade.definition(kind, name, routine_kind)
                    if not reads:
                        # El índice lo listó y el adapter ya no lo encuentra (un ``DROP`` en el
                        # medio). Se informa como ausente: omitirlo en silencio dejaría una
                        # respuesta que no dice nada de lo que se pidió.
                        missing.append((kind, name, routine_kind))
                        continue
                    for read in reads:
                        missing_body_reason = (
                            routine_body_reason(engine, server_version, proc_flag)
                            if read.kind == "routine"
                            else None
                        )
                        results.append(
                            build_definition(read, missing_body_reason=missing_body_reason)
                        )
            except Exception:
                # El detalle (timeout de sesión, error del driver) lo traduce quien llama o va al
                # log del despachador; acá solo queda el rastro de que la lectura no terminó.
                _record_definition_result(
                    actor, resuelta, ok=False, results=results, missing_count=len(missing)
                )
                raise
            _record_definition_result(
                actor, resuelta, ok=True, results=results, missing_count=len(missing)
            )
        consistent_structure = facade.consistent_structure
        session_warnings = tuple(facade.warnings)

    return DefinitionBatch(
        database=resuelta,
        results=tuple(results),
        missing=tuple(missing),
        consistent_structure=consistent_structure,
        session_warnings=session_warnings,
    )


def body_availability(actor: Actor, resuelta: AgentDatabase, facade) -> BodyAvailability:
    """
    ¿Se puede pedir el cuerpo de cada tipo con ``get_definition``? Solo con el scope del llamador y
    la versión del motor: NO ejecuta ningún ``SHOW CREATE`` (``list_objects`` es el índice barato y
    no puede leer código).

    - Sin el scope ``data.definitions`` (o con el kill switch apagado): ``scope_disabled`` en todo.
    - Con el scope: la razón por motor/versión de ``routine_body_reason`` para las RUTINAS
      (``flag_off`` o ``engine_unsupported``); vistas, triggers y events salen disponibles. Que
      "disponible" no promete un cuerpo (un privilegio puede faltar): lo dice ``get_definition``
      por objeto. Lo que no puede afirmarse sin leer, no se afirma acá.
    """
    from app.services.capability_catalog import data_capability_enabled
    from app.services.db_admin.readonly_probe import routine_body_reason

    scope_open = actor.has(Capability.DATA_DEFINITIONS) and data_capability_enabled(
        Capability.DATA_DEFINITIONS
    )
    if not scope_open:
        return BodyAvailability(
            reasons={kind: "scope_disabled" for kind in _KINDS_WITH_BODY},
            routines_may_be_hidden=False,
        )
    routine_reason = routine_body_reason(
        resuelta.database.engine, facade.server_version(), resuelta.readonly_proc_grant
    )
    reasons: dict = {kind: None for kind in _KINDS_WITH_BODY}
    reasons["routine"] = routine_reason
    return BodyAvailability(reasons=reasons, routines_may_be_hidden=routine_reason is not None)


# --------------------------------------------------------------------------- #
# Estadísticas de almacenamiento de tablas (get_table_stats)                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TableStatsBatch:
    """
    Lo que ``read_table_stats`` entrega al handler, ya sin sesión abierta.

    ``results`` son ``TableStatsRead`` en el orden del pedido. ``missing`` son los nombres pedidos
    que NO están en el índice de tablas (o que el motor dejó de resolver en el medio).
    ``row_estimates_included`` dice si ESTE llamador puede ver ``row_estimate`` y
    ``auto_increment``: el handler lo usa para elegir el modelo de salida y documentar el motivo.
    """

    database: AgentDatabase
    results: tuple
    missing: tuple[str, ...]
    row_estimates_included: bool
    consistent_structure: bool
    session_warnings: tuple[str, ...]


def caller_may_see_row_estimates(actor: Actor) -> bool:
    """
    ¿Este llamador puede ver ``row_estimate`` y ``auto_increment``? Solo con ``data.read`` Y su kill
    switch encendido (``MCP_DATA_READ_ENABLED``, leído en cada llamada).

    Es la misma frontera que ``count_rows``: ``TABLE_ROWS``/``reltuples`` aproximan un ``COUNT(*)``
    y ``AUTO_INCREMENT`` delata cuántas filas se insertaron alguna vez, así que dárselos a un token
    de solo estructura abriría por la puerta de atrás lo que el scope de datos cierra. No exige la
    credencial de datos por base: el estimado se lee del catálogo con la credencial de ESTRUCTURA.
    """
    from app.services.capability_catalog import data_capability_enabled

    return actor.has(Capability.DATA_READ) and data_capability_enabled(Capability.DATA_READ)


def read_table_stats(
    actor: Actor, database_id: int, tables: list[str], capability: Capability
) -> TableStatsBatch:
    """
    ``get_table_stats``: estadísticas de almacenamiento de hasta ``MCP_MAX_OBJECTS_PER_CALL``
    tablas de UNA base.

    Va por el gate de ESTRUCTURA (``open_readonly``), igual que ``list_objects``, y NO por
    ``_data_gate``: el scope es ``databases.read`` y lo único de "datos" que hay (el estimado de
    filas y el próximo ``AUTO_INCREMENT``) se decide acá con ``caller_may_see_row_estimates`` y,
    cuando es que no, ni se lee del motor.

    Orden: tope de tablas -> ``open_readonly`` (scope, proyecto, credencial de estructura, política)
    -> índice -> lo que no es una tabla del índice va a ``missing`` SIN consultarse -> lectura de
    las presentes. Un nombre ajeno al índice (``x'; DROP TABLE t``, ``otra_base.t``, ``_gw_v_x``,
    que el índice excluye) nunca llega a una consulta.
    """
    from app.core.environments import MCP_MAX_OBJECTS_PER_CALL

    if not tables or len(tables) > MCP_MAX_OBJECTS_PER_CALL:
        raise _deny(
            codes.CODE_TOO_MANY_OBJECTS,
            413,
            f"'tables' admite de 1 a {MCP_MAX_OBJECTS_PER_CALL} tablas por llamada.",
        )

    include_row_estimates = caller_may_see_row_estimates(actor)
    with open_readonly(actor, database_id, capability) as (resuelta, facade):
        indexed_tables = set(facade.object_index().get("table", []))
        present = [name for name in tables if name in indexed_tables]
        missing = [name for name in tables if name not in indexed_tables]
        results: list = []
        if present:
            reads = facade.table_stats(present, include_row_estimates=include_row_estimates)
            read_names = {read.table for read in reads}
            results.extend(reads)
            # El índice la listó y el catálogo ya no la devuelve (un ``DROP`` en el medio, o una
            # tabla sin privilegio): se informa como ausente y no se omite en silencio.
            missing.extend(name for name in present if name not in read_names)
        consistent_structure = facade.consistent_structure
        session_warnings = tuple(facade.warnings)

    return TableStatsBatch(
        database=resuelta,
        results=tuple(results),
        missing=tuple(missing),
        row_estimates_included=include_row_estimates,
        consistent_structure=consistent_structure,
        session_warnings=session_warnings,
    )


def draft_agent_query(actor: Actor, database_id: int, sql: str, capability: Capability) -> dict:
    """
    Clasifica el SQL que redactó un agente SIN ejecutarlo: el sobre de ``draft_query``.

    Pasa por el gate completo de la base (``resolve_agent_database``) pero **no construye ningún
    target ni abre ninguna conexión**: no llama a ``_readonly_target`` ni a ``open_readonly``. Es
    lo que distingue a ``draft_query`` de las tools que leen el catálogo, y por eso el test
    (``tests/test_mcp_draft_query.py``) parchea ``remote_engine.database_connection`` para que
    explote si alguien lo toca. Redactar nunca ejecuta, ni siquiera una lectura.

    El motor y el nombre de la base salen de la FILA de inventario (``resuelta.database``), nunca
    de un string del agente: el validador compara los nombres calificados del SQL contra ESA base.
    Los imports son perezosos por la misma razón que en el resto del módulo (el guard de
    ``tests/test_mcp_import_guard.py``).
    """
    from app.services.db_admin import agent_sql_policy as policy

    resuelta = resolve_agent_database(actor, database_id, capability)
    verdict = policy.validate_agent_select(
        sql,
        engine=resuelta.database.engine,
        database=resuelta.database.database,
        max_rows=policy.DEFAULT_MAX_ROWS,
        max_offset=policy.DEFAULT_MAX_OFFSET,
        max_bytes=policy.DEFAULT_MAX_SQL_BYTES,
    )
    return policy.build_draft_envelope(verdict, sql, max_bytes=policy.DEFAULT_MAX_SQL_BYTES)


# --------------------------------------------------------------------------- #
# Lecturas de DATOS del agente: sample_rows, distinct_values, count_rows        #
# --------------------------------------------------------------------------- #

#: Códigos internos de la política de datos que salen al agente traducidos (``public_reason``).
_DATA_GATE_CODES = frozenset(
    {
        codes.CODE_DATA_DISABLED,
        codes.CODE_DATA_NOT_OPTED_IN,
        codes.CODE_DATA_CREDENTIAL_MISSING,
        codes.CODE_DATA_PROBE_STALE,
    }
)


def _data_gate(actor: Actor, database_id: int, capability: Capability) -> AgentDatabase:
    """
    ``resolve_agent_data_database`` con la salida que ve el agente: los códigos internos de la
    política de datos (``mcp.data_*``) salen como los públicos cerrados ``DATA_DISABLED`` /
    ``PROBE_NOT_GREEN``. Los de autorización (scope, no encontrada) y los del gate de estructura
    pasan tal cual.

    El kill switch se mira PRIMERO y en cada llamada: apagado, no se lee ni el inventario.
    """
    from app.services.capability_catalog import data_capability_enabled

    if not data_capability_enabled(capability):
        raise _deny(
            codes.REASON_DATA_DISABLED,
            403,
            "Las tools de datos están apagadas en este gateway (kill switch).",
        )
    try:
        return resolve_agent_data_database(actor, database_id, capability)
    except AppHttpException as exc:
        interno = (exc.public_context or {}).get("code")
        if interno in _DATA_GATE_CODES:
            raise _deny(codes.public_reason(interno), exc.status_code, exc.message) from exc
        raise


def _data_target(resuelta: AgentDatabase):
    """
    ``(ServerTarget, QueryCredential)`` con la credencial de DATOS de la base. Es la ÚNICA función de
    este módulo que descifra el secreto de datos, y vuelve a exigir credencial presente, sonda fresca
    y opt-in aprobado (``_assert_data_credential_open``) justo antes de descifrar: entre el gate y
    acá alguien pudo revocar.

    Nunca lee ``root_password_encrypted`` ni la credencial de estructura del servidor.
    """
    from app.core.crypto import CryptoConfigError, CryptoError, decrypt
    from app.core.database import Database
    from app.core.environments import REMOTE_SSL_MODE
    from app.core.remote_engine import ServerTarget
    from app.models.managed_database_data_credential import ManagedDatabaseDataCredential
    from app.models.server import Server
    from app.services.db_admin.query_runner import MODE_STORED, QueryCredential

    session = Database().get_declarative_base_session()
    try:
        cred = (
            session.query(ManagedDatabaseDataCredential)
            .filter(
                ManagedDatabaseDataCredential.managed_database_id
                == resuelta.database.database_id
            )
            .first()
        )
        try:
            _assert_data_credential_open(cred)
        except AppHttpException as exc:
            interno = (exc.public_context or {}).get("code")
            raise _deny(codes.public_reason(interno), exc.status_code, exc.message) from exc
        srv = session.get(Server, resuelta.database.server_id)
        if srv is None:
            raise _deny(codes.REASON_PROBE_NOT_GREEN, 403, "El servidor de la base no existe.")
        try:
            password = decrypt(cred.password_encrypted)
        except (CryptoError, CryptoConfigError) as exc:
            raise _deny(
                codes.REASON_DATA_DISABLED,
                403,
                "La credencial de datos de la base no se pudo descifrar.",
            ) from exc
        username = cred.username
        target = ServerTarget(
            server_id=srv.id,
            dialect=srv.engine.value if hasattr(srv.engine, "value") else str(srv.engine),
            host=srv.host,
            port=srv.port,
            admin_user=username,
            admin_password=password,
            ssl_mode=srv.ssl_mode if srv.ssl_mode is not None else REMOTE_SSL_MODE,
        )
        return target, QueryCredential(mode=MODE_STORED, username=username, password=password)
    finally:
        session.close()


def _run_data_tool(
    actor: Actor,
    tool: str,
    database_id: int,
    capability: Capability,
    *,
    table,
    columns: list[str] | None,
    limit,
    kind: str,
) -> dict:
    """
    El camino único de las tres tools de datos. Orden (cada paso corta el siguiente):

    1. gate de datos (kill switch -> scope -> proyecto/entorno -> opt-in -> credencial -> sonda
       fresca), con los códigos públicos cerrados;
    2. identificadores contra el CATÁLOGO por ``open_readonly`` (credencial de estructura): uno
       desconocido es ``UNKNOWN_IDENTIFIER`` y la cuenta de datos nunca conecta (S12);
    3. sentencia armada sobre AST cuoteado + ``validate_agent_select`` (``limit`` clamped, nunca
       elevado);
    4. ``agent_query.run_agent_select``: ``record_intent`` -> conexión de datos READ ONLY.

    Los imports son perezosos por el guard de ``tests/test_mcp_import_guard.py``.
    """
    from app.services.db_admin import agent_query as aq

    resuelta = _data_gate(actor, database_id, capability)

    pedidas = [] if columns is None else list(columns)
    with open_readonly(actor, database_id, capability) as (_, facade):
        tabla, cols = aq.resolve_identifiers(facade, table, pedidas)

    if kind == "count":
        max_rows, warnings = 1, []
        sql = aq.build_count_rows(resuelta.database.engine, tabla)
    else:
        max_rows, warnings = aq.effective_limit(limit)
        if kind == "distinct":
            sql = aq.build_distinct_values(resuelta.database.engine, tabla, cols[0])
        else:
            sql = aq.build_sample_rows(resuelta.database.engine, tabla, cols or None)

    verdict = aq.validate_built(
        sql,
        engine=resuelta.database.engine,
        database=resuelta.database.database,
        max_rows=max_rows,
    )
    target, credential = _data_target(resuelta)
    return aq.run_agent_select(
        aq.AuditContext(
            actor=actor,
            tool=tool,
            database_id=resuelta.database.database_id,
            server_id=resuelta.database.server_id,
        ),
        resolved=resuelta.database,
        target=target,
        credential=credential,
        verdict=verdict,
        max_rows=max_rows,
        warnings=warnings,
    )


def sample_rows_query(
    actor: Actor,
    database_id: int,
    table: str,
    columns: list[str] | None,
    limit: int | None,
    capability: Capability,
) -> dict:
    """``sample_rows``: hasta ``limit`` filas de una tabla del catálogo (ver ``_run_data_tool``)."""
    return _run_data_tool(
        actor,
        "sample_rows",
        database_id,
        capability,
        table=table,
        columns=columns,
        limit=limit,
        kind="sample",
    )


def distinct_values_query(
    actor: Actor,
    database_id: int,
    table: str,
    column: str,
    limit: int | None,
    capability: Capability,
) -> dict:
    """``distinct_values``: valores distintos de UNA columna, ordenados (ver ``_run_data_tool``)."""
    return _run_data_tool(
        actor,
        "distinct_values",
        database_id,
        capability,
        table=table,
        columns=[column],
        limit=limit,
        kind="distinct",
    )


def count_rows_query(actor: Actor, database_id: int, table: str, capability: Capability) -> dict:
    """``count_rows``: ``COUNT(*)`` de una tabla del catálogo (ver ``_run_data_tool``)."""
    return _run_data_tool(
        actor,
        "count_rows",
        database_id,
        capability,
        table=table,
        columns=None,
        limit=None,
        kind="count",
    )


# --------------------------------------------------------------------------- #
# run_select: SQL libre de SOLO LECTURA, mismo gate, mismo servicio, mismo validador #
# --------------------------------------------------------------------------- #


def run_agent_select_query(
    actor: Actor, database_id: int, sql: str, limit, capability: Capability
) -> dict:
    """
    ``run_select``: ejecuta un ``SELECT`` redactado por el agente, o devuelve un borrador.

    NO es un camino nuevo hacia el motor: es el MISMO gate de datos (``_data_gate``: kill switch
    ``MCP_DATA_QUERY_ENABLED`` -> scope ``data.query`` -> proyecto/entorno -> credencial de datos con
    sonda fresca -> opt-in aprobado), el MISMO validador (``validate_agent_select``) y el MISMO
    servicio (``run_agent_select``) que las tres tools parametrizadas; solo cambia quién escribió el
    texto. Orden, y cada paso corta el siguiente:

    1. gate de datos (el kill switch se mira primero y en cada llamada);
    2. ``limit`` (ausente = ``MCP_QUERY_DEFAULT_ROWS``; por encima del máximo se recorta con
       ``LIMIT_TOO_HIGH``): solo BAJA o iguala el tope del gateway, nunca lo sube;
    3. ``validate_agent_select`` sobre el texto del agente;
    4. **todo lo que no es una lectura aceptada** (write, ddl, blocked, invalid: un ``DELETE``, un
       ``SELECT ... INTO OUTFILE``, un ``OFFSET`` enorme, un literal ilegible) vuelve como el SOBRE
       DEL BORRADOR (``classification``, ``reasons``, ``warnings``, ``query_text``,
       ``touches_engine: false``) y TERMINA ACÁ: no se descifra la credencial de datos, no hay
       conexión ni intención de auditoría de ejecución (S23, S25). Un rechazo no es un error de
       protocolo: un agente que lo recibiera reintentaría en loop;
    5. lectura aceptada: ``run_agent_select`` ejecuta ``verdict.executed_sql`` (el render de
       sqlglot del árbol ya verificado, jamás el texto crudo del agente: un ``LIMIT`` propio
       ``<= tope`` tal cual, o el tope + 1 empujado), con ``record_intent`` antes de conectar.

    Los imports son perezosos por el guard de ``tests/test_mcp_import_guard.py``.
    """
    from app.core import environments as env
    from app.services.db_admin import agent_query as aq
    from app.services.db_admin import agent_sql_policy as policy

    resuelta = _data_gate(actor, database_id, capability)
    max_rows, warnings = aq.effective_limit(limit)

    verdict = policy.validate_agent_select(
        sql,
        engine=resuelta.database.engine,
        database=resuelta.database.database,
        max_rows=max_rows,
        max_offset=env.MCP_QUERY_MAX_OFFSET,
        max_bytes=env.MCP_QUERY_MAX_SQL_BYTES,
    )
    if not verdict.accepted or verdict.executed_sql is None or verdict.row_bound is None:
        return policy.build_draft_envelope(verdict, sql, max_bytes=env.MCP_QUERY_MAX_SQL_BYTES)

    target, credential = _data_target(resuelta)
    return aq.run_agent_select(
        aq.AuditContext(
            actor=actor,
            tool="run_select",
            database_id=resuelta.database.database_id,
            server_id=resuelta.database.server_id,
        ),
        resolved=resuelta.database,
        target=target,
        credential=credential,
        verdict=verdict,
        max_rows=max_rows,
        warnings=warnings,
    )


def structural_changes(source, target) -> tuple[list[dict], bool]:
    """
    El diff de dos snapshots, PROYECTADO a lo que puede ver un agente: ``(cambios, cross_flavor)``.

    Vive acá y no en ``app/mcp`` porque el paquete del MCP no puede importar la capa de diff (ver
    ``tests/test_mcp_import_guard.py``), y porque la proyección es la parte que importa: cada
    ``DiffItem`` trae ``source_payload``/``target_payload`` con defaults, cuerpos y expresiones
    completas. Acá se queda con tipo, nombre, tabla padre, tipo de cambio, atributos que difieren
    y si es destructivo — y nada más cruza hacia la tool. No se llama a ``render_diff``: no se
    genera ni una línea de SQL.
    """
    from app.services.db_admin.schema_diff import diff_snapshots

    diff = diff_snapshots(source, target)
    cambios = [
        {
            "object_type": item.object_type,
            "object_name": item.object_name,
            "parent_table": item.parent_table,
            "change_type": item.change_type,
            "changed_attributes": list(item.changed_attributes),
            "destructive": bool(item.risk.destructive),
        }
        for item in diff.items
    ]
    return cambios, bool(diff.cross_flavor_warning)


def exclude_internal_tables(names) -> list[str]:
    """
    Quita la contabilidad interna del gateway (``_gw_v_*``/``_gw_stg_*``) de una lista de tablas.

    ``list_object_names`` ya la excluye en el adapter; esto es la SEGUNDA barrera para las tools
    del MCP, que no pueden importar ``identifiers`` (``tests/test_mcp_import_guard.py``) y no
    deben confiar en que el filtro de capa de abajo siga ahí: una tool de búsqueda que la
    listara le daría al agente la tabla de versión de Alembic como candidato de cualquier consulta.
    """
    from app.services.db_admin.identifiers import exclude_gateway_internal_tables

    return exclude_gateway_internal_tables(names)


# --------------------------------------------------------------------------- #
# Inventario operativo de lo alcanzable: entornos, exportaciones, clonados       #
# --------------------------------------------------------------------------- #
#
# Las tres funciones parten de ``reachable_databases`` y nunca de la tabla entera: lo que el agente
# ve es SIEMPRE una proyección de las bases que alcanza. Un listado de "todos los entornos" o "todos
# los exports" sería un oráculo del inventario del gateway por una vía lateral. Y devuelven dicts
# con los campos ya elegidos: las filas ORM (con ``confirm_token``, ``spec``, ``error`` del motor,
# rutas de artefactos) no salen de este módulo.

#: Tope de trabajos por listado. Los más recientes primero; pasarlo es un ERROR, no un recorte.
_MAX_JOBS = 200


def _too_many_jobs(kind: str) -> AppHttpException:
    return AppHttpException(
        message=(
            f"Hay más de {_MAX_JOBS} trabajos de {kind} sobre las bases de este token. No se "
            "trunca: una lista cortada haría creer que no hay más."
        ),
        status_code=413,
        public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
    )


def reachable_environments(actor: Actor, capability: Capability) -> list[dict]:
    """Los entornos de las bases alcanzables, con su política y cuántas de ESAS bases tienen."""
    from app.core.database import Database
    from app.models.environment import Environment

    bases = reachable_databases(actor, capability)
    conteo: dict[str, int] = {}
    for b in bases:
        if b.environment_slug:
            conteo[b.environment_slug] = conteo.get(b.environment_slug, 0) + 1
    if not conteo:
        return []
    session = Database().get_declarative_base_session()
    try:
        filas = (
            session.query(Environment)
            .filter(Environment.slug.in_(list(conteo)))
            .order_by(Environment.rank, Environment.slug)
            .all()
        )
        return [
            {
                "slug": e.slug,
                "name": e.name,
                "rank": e.rank,
                "allows_agent_access": bool(e.allows_agent_access),
                "blocks_destructive_migrations": bool(e.blocks_destructive_migrations),
                "reachable_database_count": conteo.get(e.slug, 0),
            }
            for e in filas
        ]
    finally:
        session.close()


def reachable_export_jobs(actor: Actor, capability: Capability) -> list[dict]:
    """
    Estado de los exports de bases alcanzables. **Solo estado**: ni ``spec``, ni selección
    resuelta, ni artefactos, ni ``confirm_token``, ni el texto del error.
    """
    from app.core.database import Database
    from app.models.export_job import ExportJob

    ids = [b.database_id for b in reachable_databases(actor, capability)]
    if not ids:
        return []
    session = Database().get_declarative_base_session()
    try:
        filas = (
            session.query(ExportJob)
            .filter(ExportJob.database_id.in_(ids))
            .order_by(ExportJob.id.desc())
            .limit(_MAX_JOBS + 1)
            .all()
        )
        if len(filas) > _MAX_JOBS:
            raise _too_many_jobs("exportación")
        return [
            {
                "job_id": j.id,
                "database_id": j.database_id,
                "status": j.status,
                "phase": j.phase,
                "structure_drift_detected": bool(j.structure_drift_detected),
                "has_error": bool(j.error),
                "created_at": j.created_at,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
            }
            for j in filas
        ]
    finally:
        session.close()


def reachable_clone_jobs(actor: Actor, capability: Capability) -> list[dict]:
    """
    Estado de los clonados donde ALGUNO de los dos lados es una base alcanzable. El lado que el
    token no alcanza sale como ``None``: ni su id ni su nombre, porque podría ser de otro proyecto.
    """
    from sqlalchemy import or_

    from app.core.database import Database
    from app.models.clone_job import CloneJob

    ids = {b.database_id for b in reachable_databases(actor, capability)}
    if not ids:
        return []
    session = Database().get_declarative_base_session()
    try:
        filas = (
            session.query(CloneJob)
            .filter(
                or_(
                    CloneJob.source_database_id.in_(ids),
                    CloneJob.target_database_id.in_(ids),
                )
            )
            .order_by(CloneJob.id.desc())
            .limit(_MAX_JOBS + 1)
            .all()
        )
        if len(filas) > _MAX_JOBS:
            raise _too_many_jobs("clonado")
        return [
            {
                "job_id": j.id,
                "source_database_id": (
                    j.source_database_id if j.source_database_id in ids else None
                ),
                "target_database_id": (
                    j.target_database_id if j.target_database_id in ids else None
                ),
                "status": j.status,
                "phase": j.phase,
                "include_data": bool(j.include_data),
                "has_error": bool(j.error),
                "created_at": j.created_at,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
            }
            for j in filas
        ]
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Frescura (plan 12 §6.6) y catálogos de referencia                             #
# --------------------------------------------------------------------------- #


def freshness_facts(database_id: int, version: str | None) -> dict:
    """
    Lo que el GATEWAY sabe sobre la versión que la base declara: ``trust`` y aplicación parcial.

    ``trust`` (plan 12 §6.6), sin columna nueva:

    - ``"applied"``: hay una fila ``applied`` (y no de rollback) en ``database_migration_history``
      para esta base con ``applied_version`` igual a la versión leída. Corrió DDL por el gateway.
    - ``"declared"``: hay versión pero esa fila no existe. Es lo que deja un ``stamp``, una base
      adoptada o un cambio hecho por fuera: el gateway no puede probar que corrió DDL.
    - ``"unknown"``: la base no declara versión.

    Se compara contra ``applied_version`` (la copia CONGELADA del número) y no contra la FK a la
    definición: tras un renumerado, la FK devolvería un número que esa base nunca tuvo.

    ``has_partial_application`` viaja aparte porque la versión de Alembic solo se registra cuando
    el upgrade TERMINA: una base a medio aplicar reporta la versión anterior y parece sana.

    **La base ya pasó el gate**: esta función solo la llama ``check_freshness`` después de
    ``open_readonly``, así que no repite el chequeo de alcance.
    """
    from app.core.database import Database
    from app.models.database_migration_history import DatabaseMigrationHistory
    from app.models.enums import MigrationStatus
    from app.services.db_admin.migration_progress import databases_with_incomplete_progress

    if version is None:
        trust = "unknown"
    else:
        session = Database().get_declarative_base_session()
        try:
            fila = (
                session.query(DatabaseMigrationHistory.id)
                .filter(
                    DatabaseMigrationHistory.managed_database_id == database_id,
                    DatabaseMigrationHistory.applied_version == version,
                    DatabaseMigrationHistory.status == MigrationStatus.applied,
                    (DatabaseMigrationHistory.direction.is_(None))
                    | (DatabaseMigrationHistory.direction == "up"),
                )
                .first()
            )
        finally:
            session.close()
        trust = "applied" if fila is not None else "declared"
    parciales = databases_with_incomplete_progress([database_id])
    return {"trust": trust, "has_partial_application": database_id in parciales}


def reference_catalogs(actor: Actor, capability: Capability) -> dict:
    """
    Los catálogos de REFERENCIA del gateway: privilegios por motor, charsets/collations
    habilitados y las plantillas de perfiles de permisos.

    Son datos globales del gateway, sin nada de terceros: ninguna fila nombra un servidor, una
    base, un usuario del motor ni a quién se le aplicó un perfil (las plantillas no guardan eso;
    los GRANTs reales viven en el motor). Solo lo ACTIVO: un privilegio desactivado no se puede
    otorgar, así que listarlo sería información de política sin uso.

    Las descripciones de los perfiles NO salen: son texto libre del operador y pueden nombrar
    clientes o aplicaciones. El nombre y el contenido (nivel → privilegios) son la referencia.
    """
    from app.core.database import Database
    from app.models.charset_collation_option import CharsetCollationOption
    from app.models.permission_profile import PermissionProfile, PermissionProfileItem
    from app.models.privilege import Privilege

    assert_agent_scope(actor, capability)
    session = Database().get_declarative_base_session()
    try:
        privilegios = (
            session.query(Privilege)
            .filter(Privilege.is_active.is_(True))
            .order_by(Privilege.engine, Privilege.category, Privilege.name)
            .all()
        )
        charsets = (
            session.query(CharsetCollationOption)
            .filter(CharsetCollationOption.enabled.is_(True))
            .order_by(
                CharsetCollationOption.engine_family,
                CharsetCollationOption.charset,
                CharsetCollationOption.collation,
            )
            .all()
        )
        perfiles = (
            session.query(PermissionProfile)
            .filter(PermissionProfile.is_active.is_(True))
            .order_by(PermissionProfile.engine, PermissionProfile.name)
            .all()
        )
        items: dict[int, list] = {}
        if perfiles:
            for it in (
                session.query(PermissionProfileItem)
                .filter(PermissionProfileItem.profile_id.in_([p.id for p in perfiles]))
                .order_by(PermissionProfileItem.level)
                .all()
            ):
                items.setdefault(it.profile_id, []).append(it)
        return {
            "privileges": [
                {
                    "engine": p.engine,
                    "name": p.name,
                    "category": p.category,
                    "context": p.context,
                    "description": p.description,
                    "is_sensitive": bool(p.is_sensitive),
                }
                for p in privilegios
            ],
            "charsets": [
                {
                    "engine_family": c.engine_family,
                    "charset": c.charset,
                    "collation": c.collation,
                    "is_default": bool(c.is_default),
                }
                for c in charsets
            ],
            "permission_profiles": [
                {
                    "name": p.name,
                    "engine": p.engine,
                    "items": [
                        {
                            "level": it.level,
                            "privileges": [
                                x.strip() for x in (it.privileges or "").split(",") if x.strip()
                            ],
                        }
                        for it in items.get(p.id, [])
                    ],
                }
                for p in perfiles
            ],
        }
    finally:
        session.close()
