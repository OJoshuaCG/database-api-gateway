"""
Resolvedores de destino de la capa 2: declaran QUÉ se toca, y recién después se resuelve DÓNDE.

DOS MITADES, Y POR QUÉ ESTÁN SEPARADAS
--------------------------------------
Cada tipo de destino tiene un **resolvedor puro** y una **función de puntos**:

- El resolvedor es una dependencia de FastAPI que devuelve un ``ScopeTarget`` (tipo + parámetros)
  **sin tocar la BD**. FastAPI ejecuta las sub-dependencias ANTES que la dependencia padre, o
  sea antes de autenticar y de verificar el CSRF; una consulta ahí sería un oráculo de
  existencia ("este id existe / no existe") para quien ni siquiera tiene sesión.
- La función de puntos (``_RESOLVERS``) consulta la BD y devuelve los ``ScopePoint`` donde se
  evalúa la capacidad. ``require_at`` la invoca después de autenticar, y solo si el actor tiene
  grants por alcance.

Un destino inexistente resuelve al entorno MÁS PROTEGIDO, no a un 404: para un actor con
restricciones, "no existe" y "no podés" tienen que ser indistinguibles.

``TARGET_KINDS`` mapea cada resolvedor a su tipo y es el vocabulario cerrado que el script de
cobertura valida: un resolvedor que no esté registrado falla al importar la ruta.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from fastapi import Request

from app.core.scope import (
    ScopePoint,
    ScopeTarget,
    _session,
    most_protected_environment_id,
    resolve_environment_id,
)
from app.services.capability_catalog import Capability


# --------------------------------------------------------------------------- #
# Resolvedores puros (dependencias de FastAPI): cero BD                        #
# --------------------------------------------------------------------------- #


def database(db_id: int) -> ScopeTarget:
    """Una BD del inventario, por el ``db_id`` de la ruta."""
    return ScopeTarget("database", (db_id,))


def server(server_id: int) -> ScopeTarget:
    """Un servidor entero: el entorno más protegido entre sus BDs inventariadas."""
    return ScopeTarget("server", (server_id,))


def server_database(server_id: int, database: str) -> ScopeTarget:
    """
    Una BD de un servidor por nombre: la fila del inventario si existe, si no la regla del
    servidor. Cubre las rutas que operan por referencia cruda del motor.
    """
    return ScopeTarget("server_database", (server_id, database))


def model(model_id: int) -> ScopeTarget:
    """
    Un blueprint entero: el entorno MÁS PROTEGIDO entre sus BDs (cuantificador ``all``).

    Las operaciones de blueprint escriben en cada BD que lo replica, así que se evalúan en el
    peor entorno. Un blueprint sin BDs no escribe en ningún motor: decide el rol base.
    """
    return ScopeTarget("model", (model_id,))


def server_user(user_id: int) -> ScopeTarget:
    """Un usuario del motor del inventario: el servidor de su fila (``ServerUser.server_id``)."""
    return ScopeTarget("server_user", (user_id,))


async def _body(request: Request) -> dict[str, Any]:
    """
    El JSON crudo del request, o ``{}`` si falta o está mal formado.

    Los resolvedores de payload leen el cuerpo CRUDO y no declaran el modelo Pydantic: FastAPI
    vería dos parámetros de cuerpo y los anidaría por nombre, rompiendo el contrato de OpenAPI.
    Starlette cachea ``request.json()``, así que la ruta lo vuelve a leer sin costo. Un cuerpo
    inválido no se rechaza acá (eso es un 422 de la ruta): queda sin campos y resuelve al
    entorno más protegido.
    """
    try:
        cuerpo = await request.json()
    except Exception:  # noqa: BLE001 - cualquier fallo de lectura es "sin campos"
        return {}
    return cuerpo if isinstance(cuerpo, dict) else {}


def _int_or_none(valor: Any) -> int | None:
    """Un entero real (no ``bool``) o ``None``. Todo lo demás es irresoluble."""
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def _str_or_none(valor: Any) -> str | None:
    return valor if isinstance(valor, str) and valor else None


async def database_update(db_id: int, request: Request) -> ScopeTarget:
    """
    La BD de un PATCH de inventario, con la intención de reclasificar declarada en el destino.

    Reclasificar (``environment_id`` distinto del actual) exige SOLO ``environments.write`` —lo
    valida el controller—, y no ``databases.write`` EN la BD: un ``security_officer`` acotado a
    lector en producción tiene que poder moverla. Que sea un cambio se decide contra la fila, o
    sea después de autenticar (ver ``_is_pure_reclassification``); acá solo se lee el cuerpo.

    ``solo_entorno`` (el cuerpo trae EXCLUSIVAMENTE ``environment_id``) es condición necesaria,
    no suficiente: un PATCH que además toca otro campo sigue necesitando ``databases.write``, o
    sea las DOS capacidades si el entorno cambia.
    """
    cuerpo = await _body(request)
    presente = "environment_id" in cuerpo
    return ScopeTarget(
        "database_update",
        (
            db_id,
            presente,
            cuerpo.get("environment_id") if presente else None,
            presente and set(cuerpo) == {"environment_id"},
        ),
    )


def _is_pure_reclassification(params: tuple) -> bool:
    """
    ¿El PATCH es SOLO un cambio real de entorno? Cuerpo con exclusivamente ``environment_id``,
    la fila existe y el valor (entero o ``None``) difiere del actual. Consulta la BD: solo se
    invoca después de autenticar (``require_at`` / ``resolve_target_points``).

    Reenviar el entorno actual NO es reclasificar: sin esta comparación el PATCH
    ``{"environment_id": <el mismo>}`` elegía ``environments.write`` y salteaba el alcance de la
    fila (``databases.write`` EN la BD). Un id inexistente tampoco: cae a ``databases.write`` en
    el entorno más protegido, igual que el resto de las rutas por ``db_id``. Un valor que no es
    entero ni ``None`` no se interpreta: sigue el camino de ``databases.write``.
    """
    from app.models.managed_database import ManagedDatabase

    db_id, presente, valor, solo_entorno = params
    if not (presente and solo_entorno):
        return False
    if valor is not None and _int_or_none(valor) is None:
        return False
    session = _session()
    try:
        bd = session.get(ManagedDatabase, db_id)
        return bd is not None and valor != bd.environment_id
    finally:
        session.close()


def capability_for_database_update(target: ScopeTarget) -> Capability:
    """
    La capacidad de capa 1 del PATCH de inventario: una reclasificación pura (ver
    ``_is_pure_reclassification``) es escribir política (``environments.write``); todo lo demás
    —incluido reenviar el entorno actual— es ``databases.write`` EN la fila. Sin esto el oficial
    de seguridad con rol base ``viewer`` no podía reclasificar, porque la ruta exigía
    ``databases.write`` antes de llegar al controller.

    Consulta la BD: ``require_at`` la invoca DESPUÉS de autenticar, nunca en el resolvedor.
    """
    if _is_pure_reclassification(target.params):
        return Capability.ENVIRONMENTS_WRITE
    return Capability.DATABASES_WRITE


async def payload_server(request: Request) -> ScopeTarget:
    """Un servidor declarado en el cuerpo (``server_id``). Ausente o inválido: irresoluble."""
    cuerpo = await _body(request)
    return ScopeTarget("payload_server", (_int_or_none(cuerpo.get("server_id")),))


def managed_create_for(server_id: int | None, environment_id: Any) -> ScopeTarget:
    """
    Destino de un alta/adopción a partir de los valores ya conocidos. Es lo que usan los
    escalamientos por payload (``assert_at``) de la ruta, que no pueden invocar la dependencia.

    ``environment_id`` ausente o ``None`` es "el default" (activo más protegido). Un valor que no
    es entero se marca ``"invalid"`` y resuelve al entorno más protegido.
    """
    if environment_id is None:
        env: int | str | None = None
    else:
        env = _int_or_none(environment_id) or "invalid"
    return ScopeTarget("managed_create", (server_id, env))


async def managed_create(request: Request) -> ScopeTarget:
    """
    Alta o adopción de una BD: el PEOR entre el entorno declarado (o el default) y el derivado
    del servidor. Declarar solo el entorno dejaría adoptar una BD de un servidor de producción
    declarando ``development``: una reclasificación disfrazada de alta.
    """
    cuerpo = await _body(request)
    return managed_create_for(
        _int_or_none(cuerpo.get("server_id")), cuerpo.get("environment_id")
    )


def sql_console_for(server_id: int | None, database: Any) -> ScopeTarget:
    """Servidor + base opcional: la fila del inventario si existe, si no la regla de servidor."""
    return ScopeTarget("sql_console", (server_id, _str_or_none(database)))


async def sql_console(server_id: int, request: Request) -> ScopeTarget:
    """Consola SQL: ``server_id`` de la ruta y ``database`` opcional del cuerpo."""
    cuerpo = await _body(request)
    return sql_console_for(server_id, cuerpo.get("database"))


def snapshot_source_for(server_id: int | None, database: Any) -> ScopeTarget:
    """La BD de origen de un snapshot (``server_id`` + ``database`` del cuerpo)."""
    return ScopeTarget("snapshot_source", (server_id, _str_or_none(database)))


async def snapshot_source(request: Request) -> ScopeTarget:
    cuerpo = await _body(request)
    return snapshot_source_for(_int_or_none(cuerpo.get("server_id")), cuerpo.get("database"))


def clone_job(job_id: int) -> ScopeTarget:
    """Un job de clonación persistido: AMBOS extremos (origen y destino) de su fila."""
    return ScopeTarget("clone_job", (job_id,))


def clone_job_target(job_id: int) -> ScopeTarget:
    """Un job de clonación persistido, solo su DESTINO (cancelar es una acción sobre el destino)."""
    return ScopeTarget("clone_job_target", (job_id,))


def export_job(job_id: int) -> ScopeTarget:
    """Un job de exportación persistido: la base de origen de su fila."""
    return ScopeTarget("export_job", (job_id,))


def collation_job(job_id: int) -> ScopeTarget:
    """Un job de conversión de collation persistido: la base de su fila."""
    return ScopeTarget("collation_job", (job_id,))


def comparison(comparison_id: int) -> ScopeTarget:
    """Una comparación de esquemas persistida: el DESTINO de su fila (donde se aplica el DDL)."""
    return ScopeTarget("comparison", (comparison_id,))


async def clone_create(request: Request) -> ScopeTarget:
    """
    Alta de un plan de clonación: origen Y destino del cuerpo.

    El origen es ``source_database_id`` o ``source_server_id`` + ``source_database_name``. El
    destino va SIEMPRE por servidor + nombre: ``target_database_id`` es informativo y declarar
    una BD de desarrollo ahí no puede rebajar el entorno de un destino de producción.
    """
    cuerpo = await _body(request)
    return ScopeTarget(
        "clone_create",
        (
            _int_or_none(cuerpo.get("source_database_id")),
            _int_or_none(cuerpo.get("source_server_id")),
            _str_or_none(cuerpo.get("source_database_name")),
            _int_or_none(cuerpo.get("target_server_id")),
            _str_or_none(cuerpo.get("target_database_name")),
        ),
    )


def _query_ints(request: Request, nombre: str) -> tuple[int, ...]:
    """Los enteros válidos de un query param repetido, sin duplicados. Lo inválido se ignora."""
    vistos: set[int] = set()
    for crudo in request.query_params.getlist(nombre):
        try:
            vistos.add(int(crudo))
        except ValueError:
            continue
    return tuple(sorted(vistos))


def migration_batch(model_id: int, request: Request) -> ScopeTarget:
    """
    El lote de ``apply-all``: ``database_ids`` explícitos (cuantificador ``all``: uno prohibido
    niega todo) o, sin ellos, las BDs del blueprint (``any``: el controller omite las prohibidas).
    """
    ids = _query_ints(request, "database_ids")
    entorno = _query_ints(request, "environment_id")
    return ScopeTarget(
        "migration_batch",
        (model_id, ids, entorno[0] if entorno else None),
        "all" if ids else "any",
    )


async def collation_batch_any(model_id: int, request: Request) -> ScopeTarget:
    """Crear un lote de collation: las BDs activas del blueprint son un conjunto IMPLÍCITO."""
    cuerpo = await _body(request)
    return ScopeTarget(
        "collation_batch_any", (model_id, _int_or_none(cuerpo.get("environment_id"))), "any"
    )


def collation_batch(batch_id: int) -> ScopeTarget:
    """Un lote de collation persistido: la base de CADA uno de sus jobs (ejecutar y cancelar)."""
    return ScopeTarget("collation_batch", (batch_id,))


async def clone_batch_create(request: Request) -> ScopeTarget:
    """
    Alta de un lote de clonación: las filas las NOMBRA el cliente, así que es un conjunto
    explícito (``all``) con origen y destino de cada fila. El destino va por servidor + nombre.
    """
    cuerpo = await _body(request)
    filas = cuerpo.get("rows")
    rows = tuple(
        (
            _int_or_none(r.get("source_database_id")),
            _str_or_none(r.get("source_database_name")),
            _str_or_none(r.get("target_database_name")),
        )
        for r in (filas if isinstance(filas, list) else [])
        if isinstance(r, dict)
    )
    return ScopeTarget(
        "clone_batch_create",
        (_int_or_none(cuerpo.get("source_server_id")), _int_or_none(cuerpo.get("target_server_id")), rows),
    )


def clone_batch_any(batch_id: int) -> ScopeTarget:
    """Ejecutar o reintentar un lote persistido: filas IMPLÍCITAS, el controller particiona."""
    return ScopeTarget("clone_batch_any", (batch_id,), "any")


def clone_batch(batch_id: int) -> ScopeTarget:
    """Cancelar un lote: es una acción sobre el DESTINO de cada fila."""
    return ScopeTarget("clone_batch", (batch_id,))


async def bulk_profile(user_id: int, request: Request) -> ScopeTarget:
    """
    ``apply-profile/bulk``: el usuario fija el servidor y ``databases`` son nombres que el
    cliente NOMBRA (explícito, ``all``): una sola prohibida niega el lote entero.
    """
    cuerpo = await _body(request)
    nombres = cuerpo.get("databases")
    return ScopeTarget(
        "bulk_profile",
        (user_id, tuple(n for n in (nombres if isinstance(nombres, list) else []) if _str_or_none(n))),
    )


#: resolvedor → tipo. Es lo que ``require_at`` consulta para estampar ``__gw_scope__``.
TARGET_KINDS: dict[Callable[..., ScopeTarget], str] = {
    database: "database",
    database_update: "database_update",
    model: "model",
    server: "server",
    server_database: "server_database",
    server_user: "server_user",
    payload_server: "payload_server",
    managed_create: "managed_create",
    sql_console: "sql_console",
    snapshot_source: "snapshot_source",
    clone_create: "clone_create",
    clone_job: "clone_job",
    clone_job_target: "clone_job_target",
    export_job: "export_job",
    collation_job: "collation_job",
    comparison: "comparison",
    migration_batch: "migration_batch",
    collation_batch_any: "collation_batch_any",
    collation_batch: "collation_batch",
    clone_batch_create: "clone_batch_create",
    clone_batch_any: "clone_batch_any",
    clone_batch: "clone_batch",
    bulk_profile: "bulk_profile",
}


# --------------------------------------------------------------------------- #
# Funciones de puntos: consultan la BD, solo después de autenticar             #
# --------------------------------------------------------------------------- #


def _points_database(params: tuple) -> list[ScopePoint]:
    from app.models.managed_database import ManagedDatabase

    (db_id,) = params
    session = _session()
    try:
        bd = session.get(ManagedDatabase, db_id)
        server_id = bd.server_id if bd else None
    finally:
        session.close()
    env_id = resolve_environment_id(server_id=None, managed_database_id=db_id)
    return [ScopePoint(environment_id=env_id, server_id=server_id, item_id=db_id)]


def _points_database_update(params: tuple) -> list[ScopePoint]:
    # Reclasificación PURA: ningún punto, o sea el rol base. La autoridad es
    # ``environments.write`` (capa 1 y controller). Cualquier otro caso —otro campo, el mismo
    # entorno, un id inexistente— evalúa ``databases.write`` en la BD como siempre.
    if _is_pure_reclassification(params):
        return []
    return _points_database((params[0],))


def _points_model(params: tuple) -> list[ScopePoint]:
    from app.models.database_model import DatabaseModel
    from app.models.managed_database import ManagedDatabase

    (model_id,) = params
    session = _session()
    try:
        existe = session.get(DatabaseModel, model_id) is not None
        filas = (
            session.query(
                ManagedDatabase.id, ManagedDatabase.server_id, ManagedDatabase.environment_id
            )
            .filter(ManagedDatabase.model_id == model_id)
            .all()
        )
    finally:
        session.close()
    if not existe:
        # Un blueprint inexistente no es "sin BDs": es indistinguible de "no podés" (fail-closed).
        return _unresolvable()
    # Sin BDs devuelve [] a propósito: no hay escritura remota y ``assert_layer2`` cae al rol base.
    # Una BD sin entorno resuelve al más protegido, igual que en ``_points_database``.
    return [
        ScopePoint(
            environment_id=env if env is not None else most_protected_environment_id(),
            server_id=sid,
            item_id=db_id,
        )
        for (db_id, sid, env) in filas
    ]


def _points_server(params: tuple) -> list[ScopePoint]:
    (server_id,) = params
    env_id = resolve_environment_id(server_id=server_id, managed_database_id=None)
    return [ScopePoint(environment_id=env_id, server_id=server_id)]


def _points_server_database(params: tuple) -> list[ScopePoint]:
    from app.models.managed_database import ManagedDatabase

    server_id, nombre = params
    session = _session()
    try:
        filas = (
            session.query(ManagedDatabase.id)
            .filter(
                ManagedDatabase.server_id == server_id, ManagedDatabase.name == nombre
            )
            .all()
        )
    finally:
        session.close()
    if not filas:
        # Sin fila de inventario: regla de servidor (F-17, solo BDs inventariadas).
        return _points_server((server_id,))
    return [
        ScopePoint(
            environment_id=resolve_environment_id(
                server_id=None, managed_database_id=f[0]
            ),
            server_id=server_id,
            item_id=f[0],
        )
        for f in filas
    ]


def _most_protected_active_environment_id() -> int | None:
    """
    El entorno ACTIVO más protegido: el mismo que ``resolve_for_assignment`` asigna a un alta
    sin ``environment_id``. Sin ninguno activo cae al más protegido a secas (fail-closed).
    """
    from app.models.environment import Environment

    session = _session()
    try:
        fila = (
            session.query(Environment.id)
            .filter(Environment.is_active.is_(True))
            .order_by(Environment.rank.desc(), Environment.id.desc())
            .first()
        )
    finally:
        session.close()
    return fila[0] if fila else most_protected_environment_id()


def _points_server_user(params: tuple) -> list[ScopePoint]:
    from app.models.server_user import ServerUser

    (user_id,) = params
    session = _session()
    try:
        fila = session.query(ServerUser.server_id).filter(ServerUser.id == user_id).first()
    finally:
        session.close()
    if fila is None:
        # Sin fila: el entorno más protegido, indistinguible de "no podés".
        return [ScopePoint(environment_id=most_protected_environment_id(), server_id=None)]
    return _points_server((fila[0],))


def _points_payload_server(params: tuple) -> list[ScopePoint]:
    (server_id,) = params
    if server_id is None:
        return [ScopePoint(environment_id=most_protected_environment_id(), server_id=None)]
    return _points_server((server_id,))


def _points_managed_create(params: tuple) -> list[ScopePoint]:
    from app.models.managed_database import ManagedDatabase

    server_id, env = params
    if env is None:
        declarado = _most_protected_active_environment_id()
    elif isinstance(env, int):
        declarado = env
    else:
        declarado = most_protected_environment_id()
    puntos = [ScopePoint(environment_id=declarado, server_id=server_id)]

    if server_id is None:
        puntos.append(ScopePoint(environment_id=most_protected_environment_id(), server_id=None))
        return puntos
    session = _session()
    try:
        hay_inventario = (
            session.query(ManagedDatabase.id)
            .filter(ManagedDatabase.server_id == server_id)
            .first()
            is not None
        )
    finally:
        session.close()
    # Un servidor sin BDs inventariadas no aporta entorno propio: solo manda el declarado. Si
    # aportara "el más protegido", ningún actor acotado podría dar de alta la PRIMERA BD de un
    # servidor nuevo ni declarando el entorno donde opera (F-17: la regla es solo inventario).
    if hay_inventario:
        puntos.append(_points_server((server_id,))[0])
    return puntos


def _points_sql_console(params: tuple) -> list[ScopePoint]:
    server_id, nombre = params
    if server_id is None:
        return _points_payload_server((None,))
    if nombre is None:
        return _points_server((server_id,))
    return _points_server_database((server_id, nombre))


def _unresolvable() -> list[ScopePoint]:
    """Destino irresoluble o inexistente: el entorno más protegido (fail-closed)."""
    return [ScopePoint(environment_id=most_protected_environment_id(), server_id=None)]


def _points_ref(
    server_id: int | None, database_id: int | None, nombre: str | None
) -> list[ScopePoint]:
    """
    Una base referida por la fila de un job o por el cuerpo: ``database_id`` si apunta a una fila
    viva, si no servidor + nombre (fila del inventario o regla de servidor). Sin servidor ni
    nombre es irresoluble.
    """
    from app.models.managed_database import ManagedDatabase

    if database_id is not None:
        session = _session()
        try:
            bd = session.get(ManagedDatabase, database_id)
        finally:
            session.close()
        if bd is not None:
            return _points_database((database_id,))
    if server_id is None or nombre is None:
        return _unresolvable()
    return _points_server_database((server_id, nombre))


def _points_clone_create(params: tuple) -> list[ScopePoint]:
    src_db, src_sid, src_name, tgt_sid, tgt_name = params
    origen = _points_ref(src_sid, src_db, src_name)
    destino = _points_ref(tgt_sid, None, tgt_name)
    return origen + destino


def _clone_row(job_id: int):
    from app.models.clone_job import CloneJob

    session = _session()
    try:
        return session.query(
            CloneJob.source_server_id,
            CloneJob.source_database_id,
            CloneJob.source_database_name,
            CloneJob.target_server_id,
            CloneJob.target_database_name,
        ).filter(CloneJob.id == job_id).first()
    finally:
        session.close()


def _points_clone_job(params: tuple) -> list[ScopePoint]:
    (job_id,) = params
    fila = _clone_row(job_id)
    if fila is None:
        return _unresolvable()
    s_sid, s_db, s_name, t_sid, t_name = fila
    return _points_ref(s_sid, s_db, s_name) + _points_ref(t_sid, None, t_name)


def _points_clone_job_target(params: tuple) -> list[ScopePoint]:
    (job_id,) = params
    fila = _clone_row(job_id)
    if fila is None:
        return _unresolvable()
    return _points_ref(fila[3], None, fila[4])


def _points_export_job(params: tuple) -> list[ScopePoint]:
    from app.models.export_job import ExportJob

    (job_id,) = params
    session = _session()
    try:
        fila = (
            session.query(ExportJob.server_id, ExportJob.database_id, ExportJob.database_name)
            .filter(ExportJob.id == job_id)
            .first()
        )
    finally:
        session.close()
    return _unresolvable() if fila is None else _points_ref(fila[0], fila[1], fila[2])


def _points_collation_job(params: tuple) -> list[ScopePoint]:
    from app.models.collation_conversion_job import CollationConversionJob as Job

    (job_id,) = params
    session = _session()
    try:
        fila = (
            session.query(Job.server_id, Job.database_id, Job.database_name)
            .filter(Job.id == job_id)
            .first()
        )
    finally:
        session.close()
    return _unresolvable() if fila is None else _points_ref(fila[0], fila[1], fila[2])


def _points_comparison(params: tuple) -> list[ScopePoint]:
    from app.models.schema_comparison import SchemaComparison

    (comparison_id,) = params
    session = _session()
    try:
        fila = (
            session.query(
                SchemaComparison.target_server_id,
                SchemaComparison.target_database_id,
                SchemaComparison.target_database_name,
            )
            .filter(SchemaComparison.id == comparison_id)
            .first()
        )
    finally:
        session.close()
    return _unresolvable() if fila is None else _points_ref(fila[0], fila[1], fila[2])


# --------------------------------------------------------------------------- #
# Lotes: puntos por ítem (los controllers los reusan para particionar)         #
# --------------------------------------------------------------------------- #


def points_for_databases(rows) -> list[ScopePoint]:
    """
    Un punto por BD del inventario (``item_id`` = su id), para particionar un lote. Acepta filas
    de consulta u objetos ORM con ``id``/``server_id``/``environment_id``. Una BD sin entorno
    resuelve al más protegido, igual que en ``_points_database``.
    """
    protegido: int | None = None
    puntos: list[ScopePoint] = []
    for r in rows:
        entorno = r.environment_id
        if entorno is None:
            protegido = protegido if protegido is not None else most_protected_environment_id()
            entorno = protegido
        puntos.append(ScopePoint(environment_id=entorno, server_id=r.server_id, item_id=r.id))
    return puntos


def clone_item_points(
    source_server_id: int | None,
    target_server_id: int | None,
    items,
    *,
    target_only: bool = False,
) -> list[ScopePoint]:
    """
    Los puntos de cada fila de un lote de clonación, con el id de la FILA como ``item_id``: una
    fila se permite solo si se permiten sus DOS extremos (o solo el destino, al cancelar).
    ``items``: tuplas ``(item_id, source_database_id, source_database_name, target_database_name)``.
    """
    puntos: list[ScopePoint] = []
    for item_id, src_db, src_name, tgt_name in items:
        extremos = _points_ref(target_server_id, None, tgt_name)
        if not target_only:
            extremos = _points_ref(source_server_id, src_db, src_name) + extremos
        puntos.extend(replace(p, item_id=item_id) for p in extremos)
    return puntos


def _model_database_rows(
    model_id: int,
    *,
    ids: tuple[int, ...] = (),
    environment_id: int | None = None,
    active_only: bool = False,
):
    """``(existe, filas)``: las BDs del blueprint con los filtros de un lote, por id ascendente."""
    from app.models.database_model import DatabaseModel
    from app.models.enums import ProvisionStatus
    from app.models.managed_database import ManagedDatabase as MD

    session = _session()
    try:
        existe = session.get(DatabaseModel, model_id) is not None
        q = session.query(MD.id, MD.server_id, MD.environment_id).filter(MD.model_id == model_id)
        if active_only:
            q = q.filter(MD.status == ProvisionStatus.active)
        if ids:
            q = q.filter(MD.id.in_(ids))
        elif environment_id is not None:
            q = q.filter(MD.environment_id == environment_id)
        return existe, q.order_by(MD.id.asc()).all()
    finally:
        session.close()


def _points_migration_batch(params: tuple) -> list[ScopePoint]:
    model_id, ids, entorno = params
    existe, filas = _model_database_rows(model_id, ids=ids, environment_id=entorno)
    if not existe:
        return _unresolvable()
    puntos = points_for_databases(filas)
    # Un id explícito que no es del blueprint resuelve al entorno más protegido: un actor
    # acotado recibe 403 y no el 422 que distinguiría "existe en otro blueprint" de "no podés".
    for ajeno in sorted(set(ids) - {f.id for f in filas}):
        puntos.append(replace(_unresolvable()[0], item_id=ajeno))
    return puntos


def _points_collation_batch_any(params: tuple) -> list[ScopePoint]:
    model_id, entorno = params
    existe, filas = _model_database_rows(model_id, environment_id=entorno, active_only=True)
    return points_for_databases(filas) if existe else _unresolvable()


def _points_collation_batch(params: tuple) -> list[ScopePoint]:
    from app.models.collation_conversion_job import CollationConversionJob as Job

    (batch_id,) = params
    session = _session()
    try:
        filas = (
            session.query(Job.id, Job.server_id, Job.database_id, Job.database_name)
            .filter(Job.batch_id == batch_id)
            .all()
        )
    finally:
        session.close()
    if not filas:
        return _unresolvable()
    puntos: list[ScopePoint] = []
    for f in filas:
        puntos.extend(
            replace(p, item_id=f.id) for p in _points_ref(f.server_id, f.database_id, f.database_name)
        )
    return puntos


def _points_clone_batch_create(params: tuple) -> list[ScopePoint]:
    src_sid, tgt_sid, rows = params
    if not rows:
        return _points_payload_server((src_sid,)) + _points_payload_server((tgt_sid,))
    puntos: list[ScopePoint] = []
    for src_db, src_name, tgt_name in rows:
        puntos += _points_ref(src_sid, src_db, src_name)
        puntos += _points_ref(tgt_sid, None, tgt_name or src_name)
    return puntos


def _clone_batch_rows(batch_id: int):
    """``(servers, items)`` de un lote persistido; ``servers`` es ``None`` si no existe."""
    from app.models.clone_batch import CloneBatch, CloneBatchItem

    session = _session()
    try:
        cab = (
            session.query(CloneBatch.source_server_id, CloneBatch.target_server_id)
            .filter(CloneBatch.id == batch_id)
            .first()
        )
        items = (
            session.query(
                CloneBatchItem.id,
                CloneBatchItem.source_database_id,
                CloneBatchItem.source_database_name,
                CloneBatchItem.target_database_name,
            )
            .filter(CloneBatchItem.batch_id == batch_id)
            .order_by(CloneBatchItem.seq.asc())
            .all()
        )
    finally:
        session.close()
    return cab, [tuple(i) for i in items]


def _points_clone_batch_any(params: tuple) -> list[ScopePoint]:
    (batch_id,) = params
    cab, items = _clone_batch_rows(batch_id)
    if cab is None or not items:
        return _unresolvable()
    return clone_item_points(cab[0], cab[1], items)


def _points_clone_batch(params: tuple) -> list[ScopePoint]:
    (batch_id,) = params
    cab, items = _clone_batch_rows(batch_id)
    if cab is None or not items:
        return _unresolvable()
    return clone_item_points(cab[0], cab[1], items, target_only=True)


def _points_bulk_profile(params: tuple) -> list[ScopePoint]:
    from app.models.server_user import ServerUser

    user_id, nombres = params
    session = _session()
    try:
        fila = session.query(ServerUser.server_id).filter(ServerUser.id == user_id).first()
    finally:
        session.close()
    if fila is None:
        return _unresolvable()
    if not nombres:
        return _points_server((fila[0],))
    puntos: list[ScopePoint] = []
    for nombre in nombres:
        puntos += _points_server_database((fila[0], nombre))
    return puntos


#: tipo → función de puntos. Sus claves son el vocabulario cerrado de tipos de destino.
_RESOLVERS: dict[str, Callable[[tuple], list[ScopePoint]]] = {
    "database": _points_database,
    "database_update": _points_database_update,
    "model": _points_model,
    "server": _points_server,
    "server_database": _points_server_database,
    "server_user": _points_server_user,
    "payload_server": _points_payload_server,
    "managed_create": _points_managed_create,
    "sql_console": _points_sql_console,
    "snapshot_source": _points_sql_console,
    "clone_create": _points_clone_create,
    "clone_job": _points_clone_job,
    "clone_job_target": _points_clone_job_target,
    "export_job": _points_export_job,
    "collation_job": _points_collation_job,
    "comparison": _points_comparison,
    "migration_batch": _points_migration_batch,
    "collation_batch_any": _points_collation_batch_any,
    "collation_batch": _points_collation_batch,
    "clone_batch_create": _points_clone_batch_create,
    "clone_batch_any": _points_clone_batch_any,
    "clone_batch": _points_clone_batch,
    "bulk_profile": _points_bulk_profile,
}


def resolve_target_points(target: ScopeTarget) -> list[ScopePoint]:
    """Despacha por tipo. Un tipo desconocido es fail-closed: el entorno más protegido."""
    fn = _RESOLVERS.get(target.kind)
    if fn is None:
        return [ScopePoint(environment_id=most_protected_environment_id(), server_id=None)]
    return fn(target.params)
