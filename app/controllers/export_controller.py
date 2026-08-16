"""
Controller de exportación de bases de datos (módulo 10, fase F2 — planificación).

Exporta el contenido de una BD —estructura, datos o ambos— a un artefacto descargable, en
modo **estrictamente de solo lectura** sobre el origen: cualquier sentencia destructiva
existe únicamente como TEXTO dentro del artefacto.

Este archivo cubre la MITAD DE PLANIFICACIÓN del pipeline, que no escribe nada en el motor
y no genera ningún artefacto:

    capacidades → crear PLAN → catálogo → resolver selección → PREVIEW (congela + token)

La generación (writer), la ejecución asíncrona (runner), el almacenamiento y la descarga
llegan en F3/F4. Mientras tanto un job creado acá se queda en ``pending`` y su artefacto no
existe: es deliberado, no un pendiente olvidado.

Patrón "por identidad": la BD se referencia por ``(server_id, database_name)``, funcione o
no adoptada en el inventario — igual que collation-conversion y que las referencias crudas
de schema-comparisons. Si además está en ``managed_databases``, el id se guarda como dato
informativo, pero nada del flujo depende de que lo esté.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from app.controllers.clone_controller import _snapshot_fingerprint
from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    EXPORT_ARTIFACT_TTL_MINUTES,
    EXPORT_BATCH_ROWS,
    EXPORT_ENABLED,
    EXPORT_INLINE_MAX_BYTES,
    EXPORT_MAX_CONCURRENT_GLOBAL,
    EXPORT_MAX_DURATION_SECONDS,
    EXPORT_MAX_PARTS,
    EXPORT_MAX_STATEMENT_BYTES,
    EXPORT_ROWS_PER_STATEMENT,
    EXPORT_SINGLE_USE_DOWNLOAD,
    EXPORT_TTL_HOURS,
)
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.export_job import (
    EXPORT_ARTIFACT_AVAILABLE,
    EXPORT_ARTIFACT_CONSUMED,
    EXPORT_ITEM_ERROR,
    EXPORT_PHASE_BODIES,
    EXPORT_PHASE_DATA,
    EXPORT_PHASE_DONE,
    EXPORT_PHASE_PREAMBLE,
    EXPORT_PHASE_PREREQUISITES,
    EXPORT_PHASE_STRUCTURE,
    EXPORT_STATUS_CANCELED,
    EXPORT_STATUS_FAILED,
    EXPORT_STATUS_INTERRUPTED,
    EXPORT_STATUS_PENDING,
    EXPORT_STATUS_RUNNING,
    EXPORT_STATUS_SUCCEEDED,
    ExportJob,
    ExportJobItem,
)
from app.models.managed_database import ManagedDatabase
from app.services import audit, export_package, export_storage
from app.services.db_admin import clone_dependencies as cdeps
from app.services.db_admin import export_spec as espec
from app.services.db_admin import export_writer as ewriter
from app.services.db_admin import query_policy
from app.services.db_admin.dtos import SchemaSnapshot, TableSchema
from app.services.db_admin.export_session import ExportDurationExceeded, export_session
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import (
    ensure_not_reserved_database,
    is_gateway_internal_table,
    validate_identifier,
)
from app.services.db_admin.schema_diff import _BODY_TYPE_ORDER, _STEP, _table_dep_order

logger = get_logger(__name__)

# Familia MySQL: comparte el límite de consistencia estructural del §6.2.
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})

# Objetos cuyo DDL es un CUERPO que puede nombrar a otros objetos. Se ordenan entre sí por
# dependencia real, no alfabéticamente.
_BODY_TYPES = frozenset({"view", "materialized_view", "routine", "trigger", "event"})

# Fase legible de cada tipo. El ORDEN real lo manda ``_STEP`` (paso fino); esto es una
# etiqueta para la interfaz — el mismo criterio con el que schema-comparisons degradó
# ``phase`` a informativa cuando se descubrió que ordenar por fase producía un orden que el
# motor rechaza.
_PHASE_BY_TYPE: dict[str, str] = {
    "extension": EXPORT_PHASE_PREREQUISITES,
    "enum_type": EXPORT_PHASE_PREREQUISITES,
    "sequence": EXPORT_PHASE_PREREQUISITES,
    "table": EXPORT_PHASE_STRUCTURE,
    "view": EXPORT_PHASE_BODIES,
    "materialized_view": EXPORT_PHASE_BODIES,
    "routine": EXPORT_PHASE_BODIES,
    "trigger": EXPORT_PHASE_BODIES,
    "event": EXPORT_PHASE_BODIES,
}

# Ancho NOMINAL en bytes de un valor, por familia de tipo, para estimar el tamaño del
# artefacto. Es una aproximación GRUESA y así se declara en la respuesta: el catálogo no
# expone el tamaño real de una tabla (ningún método del adapter lo devuelve hoy), y medirlo
# de verdad exigiría recorrer el origen — que es exactamente lo que el preview no debe
# hacer. Sirve para decidir si la entrega en línea es viable y para avisar de un artefacto
# enorme, no como cifra exacta.
_NOMINAL_BYTES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("bool", "bit"), 5),
    (("tinyint", "smallint", "mediumint"), 6),
    (("bigint", "int", "serial"), 11),
    (("decimal", "numeric", "float", "double", "real"), 14),
    (("timestamp", "datetime"), 21),
    (("date", "time", "year"), 12),
    (("uuid",), 38),
    (("blob", "bytea", "text", "json", "jsonb", "xml"), 256),
)
_NOMINAL_DEFAULT = 32
# Sobrecarga por columna en el literal (comas, comillas) y por sentencia (``INSERT INTO``).
_ROW_OVERHEAD_PER_COLUMN = 3
_ROW_OVERHEAD = 8
# Coste nominal del DDL de un objeto de estructura, para que la estimación no ignore por
# completo la parte no-datos del artefacto.
_DDL_NOMINAL_BYTES = 512

# Cada cuánto (segundos) se persiste el progreso de una corrida a la BD del gateway. Sin
# throttle, una tabla de millones de filas dispara un UPDATE por trozo emitido y martilla la
# BD de metadatos — mismo criterio y mismo valor que la fase de datos del clon.
_PROGRESS_PERSIST_SECONDS = 3.0

# Cada cuántos trozos emitidos se comprueban cancelación y plazo. El writer rinde trozos
# pequeños (una sentencia, un lote de filas), así que comprobarlo en cada uno agregaría una
# consulta cacheada y un ``time.monotonic()`` por sentencia sin ganar reactividad real.
_CANCEL_CHECK_EVERY = 25


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Catálogo y orden de emisión — helpers sin motor                              #
# --------------------------------------------------------------------------- #


def _iter_catalog(snap: SchemaSnapshot):
    """
    Enumera ``(object_type, name)`` de todos los objetos de primer nivel del snapshot.

    Mismo recorrido y MISMO vocabulario de tipos que ``clone_controller._iter_objects`` y
    ``clone_dependencies``: si acá dijéramos ``type`` donde el resto del proyecto dice
    ``enum_type``, el cierre de dependencias no encontraría los objetos por clave y fallaría
    en silencio (devolvería "no existe" para algo que sí está).
    """
    for t in snap.tables:
        yield "table", t.table
    for v in snap.views:
        yield ("materialized_view" if v.is_materialized else "view"), v.name
    for r in snap.routines:
        yield "routine", r.name
    for tg in snap.triggers:
        yield "trigger", tg.name
    for s in snap.sequences:
        yield "sequence", s.name
    for e in snap.enum_types:
        yield "enum_type", e.name
    for x in snap.extensions:
        yield "extension", x.name
    for ev in snap.events:
        yield "event", ev.name


def _catalog_objects(snap: SchemaSnapshot) -> list[espec.CatalogObject]:
    """El catálogo como lo consume ``export_spec.resolve_selection``."""
    return [espec.CatalogObject(object_type=t, name=n) for t, n in _iter_catalog(snap)]


def _emission_step(object_type: str) -> int:
    """
    Paso fino de emisión de un tipo de objeto.

    Se toma de ``schema_diff._STEP`` con ``change_type='new'`` porque un artefacto de
    exportación es, en esencia, "crear todo desde cero": el mismo orden que el diff usa
    para el camino aditivo. Reusarlo —en vez de escribir una segunda tabla de fases— es lo
    que evita repetir los errores que ese orden ya tiene resueltos y documentados
    (prerrequisitos antes que tablas, FKs al final del bloque aditivo, índices después de
    las vistas materializadas en PostgreSQL).
    """
    return _STEP.get((object_type, "new"), 999)


def _cycle_nodes(nodes: list, deps: dict) -> list:
    """
    Nodos que NO se pueden ordenar topológicamente (participan en un ciclo).

    Se reporta, no se corrige: un ciclo de dependencias en el artefacto significa que
    ningún orden de emisión lo hace ejecutable de un tirón, y el operador tiene que
    saberlo ANTES de generar el archivo — no descubrirlo a mitad de una restauración.
    """
    placed: set = set()
    remaining = list(nodes)
    while remaining:
        ready = [n for n in remaining if deps.get(n, set()) <= placed]
        if not ready:
            break
        for n in ready:
            remaining.remove(n)
        placed.update(ready)
    return remaining


def _body_dependency_map(
    snap: SchemaSnapshot, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """
    Dependencias entre objetos con CUERPO, dentro del conjunto ``keys``.

    Sale del escaneo best-effort de ``clone_dependencies`` (vista que lee de otra vista,
    rutina que llama a otra rutina). Es advisory por naturaleza —sqlglot no parsea de forma
    fiable estos cuerpos—, pero un falso positivo solo reordena dos objetos independientes,
    mientras que ignorarlo produce un ``CREATE VIEW`` sobre una vista que todavía no existe.
    """
    _auth, advisory = cdeps.build_graph(snap)
    deps: dict[tuple[str, str], set[tuple[str, str]]] = {k: set() for k in keys}
    for edge in advisory:
        src = (edge.from_type, edge.from_name)
        dst = (edge.to_type, edge.to_name)
        if src in keys and dst in keys and src != dst:
            deps[src].add(dst)
    return deps


def _order_for_emission(
    snap: SchemaSnapshot, objects: list[espec.CatalogObject]
) -> tuple[list[tuple[int, espec.CatalogObject]], list[str]]:
    """
    Ordena los objetos por ORDEN DE EMISIÓN real y devuelve ``(pares (step, obj), avisos)``.

    Tres criterios, en este orden:

    1. **paso fino** (``_STEP``): prerrequisitos → tablas → objetos con cuerpo;
    2. dentro del paso, **rango topológico**: FK (tabla referida antes que la referente,
       reusando ``_table_dep_order``, la misma función que ordena el clon y la copia de
       datos) o referencia de cuerpo;
    3. desempate estable: ``_BODY_TYPE_ORDER`` para los cuerpos —rutinas antes que vistas,
       porque una vista puede llamar a una función y PostgreSQL la valida al crearla— y el
       nombre para todo lo demás.

    El §8.4 del diseño lista las vistas antes que las rutinas. Se sigue el criterio del
    repositorio (rutinas primero) a propósito: es el que ya está en ``_BODY_TYPE_ORDER`` y
    en ``snapshot_layout._CLASS_ORDER``, y llegó ahí por un fallo real en PostgreSQL. Dos
    órdenes distintos para lo mismo en el mismo proyecto es cómo se reintroduce ese fallo.
    """
    warnings: list[str] = []
    tables_by_name = {t.table: t for t in snap.tables}
    table_names = [o.name for o in objects if o.object_type == "table"]
    table_rank = _table_dep_order(table_names, tables_by_name)

    # Ciclo de FKs dentro de la selección: ningún orden de CREATE TABLE lo satisface.
    fk_deps = {
        n: {
            fk.referred_table
            for fk in tables_by_name[n].foreign_keys
            if fk.referred_table in set(table_names) and fk.referred_table != n
        }
        for n in table_names
        if n in tables_by_name
    }
    cyclic_tables = _cycle_nodes(table_names, fk_deps)
    if cyclic_tables:
        warnings.append(
            "Ciclo de claves foráneas entre "
            f"{', '.join(sorted(cyclic_tables)[:5])}: el artefacto necesita las FKs "
            "diferidas (sanitize.constraints_placement='deferred') para poder ejecutarse."
        )

    body_keys = {(o.object_type, o.name) for o in objects if o.object_type in _BODY_TYPES}
    body_deps = _body_dependency_map(snap, body_keys) if body_keys else {}
    body_rank: dict[tuple[str, str], int] = {}
    if body_keys:
        remaining = sorted(body_keys)
        placed: set = set()
        level = 0
        while remaining:
            ready = [k for k in remaining if body_deps.get(k, set()) <= placed]
            if not ready:
                break
            for k in ready:
                body_rank[k] = level
                remaining.remove(k)
            placed.update(ready)
            level += 1
        for k in remaining:  # ciclo: al final, estable
            body_rank[k] = level
        cyclic_bodies = _cycle_nodes(sorted(body_keys), body_deps)
        if cyclic_bodies:
            warnings.append(
                "Ciclo de referencias entre objetos con cuerpo "
                f"({', '.join(f'{t}:{n}' for t, n in cyclic_bodies[:5])}): el artefacto "
                "puede requerir reordenarlos o crearlos en dos pasadas."
            )

    def _sort_key(obj: espec.CatalogObject):
        step = _emission_step(obj.object_type)
        if obj.object_type == "table":
            return (step, table_rank.get(obj.name, 0), 0, obj.name)
        if obj.object_type in _BODY_TYPES:
            key = (obj.object_type, obj.name)
            return (
                step,
                body_rank.get(key, 0),
                _BODY_TYPE_ORDER.get(obj.object_type, 9),
                obj.name,
            )
        return (step, 0, 0, obj.name)

    ordered = sorted(objects, key=_sort_key)
    return [(_emission_step(o.object_type), o) for o in ordered], warnings


def _nominal_row_bytes(table: TableSchema) -> int:
    """Ancho nominal de una fila renderizada. Aproximación grueso, ver ``_NOMINAL_BYTES``."""
    total = _ROW_OVERHEAD
    for col in table.columns:
        # Las columnas GENERADAS no viajan en los INSERT (incluirlas produce un script que
        # el motor rechaza), así que tampoco pesan en la estimación.
        if col.computed is not None:
            continue
        lowered = (col.type or "").lower()
        width = _NOMINAL_DEFAULT
        for names, nominal in _NOMINAL_BYTES:
            if any(n in lowered for n in names):
                width = nominal
                break
        total += width + _ROW_OVERHEAD_PER_COLUMN
    return total


# --------------------------------------------------------------------------- #
# Controller                                                                   #
# --------------------------------------------------------------------------- #


class ExportController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Guards y carga                                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _guard_enabled() -> None:
        """
        Kill switch del módulo.

        Una exportación es por definición una EXTRACCIÓN masiva de datos en claro (no hay
        enmascarado, §9.6), así que la vía de salida tiene que poder cerrarse sin
        re-desplegar código ni tocar cada plan.
        """
        if not EXPORT_ENABLED:
            raise AppHttpException(
                message="La exportación de bases de datos está deshabilitada en este gateway.",
                status_code=409,
                public_context={"code": "export.disabled"},
            )

    def _load_context(self, server_id: int, database: str):
        """``(dialect, target, managed_id)`` — cierra la sesión ANTES de tocar el motor."""
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            target = build_target(server)
            managed = (
                session.query(ManagedDatabase)
                .filter(
                    ManagedDatabase.server_id == server_id,
                    ManagedDatabase.name == database,
                )
                .first()
            )
            return dialect, target, (managed.id if managed else None)
        finally:
            session.close()

    def _job_or_404(self, session, job_id: int) -> ExportJob:
        job = session.get(ExportJob, job_id)
        if job is None:
            raise AppHttpException(
                message="Job de exportación no encontrado.",
                status_code=404,
                context={"export_job_id": job_id},
            )
        return job

    @staticmethod
    def _assert_not_expired(job: ExportJob) -> None:
        if job.expires_at < _utcnow():
            raise AppHttpException(
                message="El plan de exportación expiró; vuelve a crearlo.",
                status_code=410,
                public_context={
                    "code": espec.CODE_ARTIFACT_EXPIRED,
                    "field": "job_id",
                },
                context={"export_job_id": job.id},
            )

    @staticmethod
    def _guard_still_pending(job: ExportJob, action: str) -> None:
        """
        El job tiene que seguir siendo un PLAN. 409 si ya se lanzó, terminó o se canceló.

        Un job es de un solo uso: su ``spec`` y su selección congelada son el registro de
        qué se exportó. Reescribirlos después de la ejecución no reabre nada útil y sí
        rompe la trazabilidad del artefacto ya entregado.
        """
        if job.status != EXPORT_STATUS_PENDING:
            raise AppHttpException(
                message=(
                    f"El job ya está en estado '{job.status}'; no se puede {action}. "
                    "Creá un plan nuevo."
                ),
                status_code=409,
                public_context={
                    "code": "export.already_executed",
                    "status": job.status,
                },
                context={"export_job_id": job.id},
            )

    def _job_target(self, job: ExportJob):
        """Reconstruye el ``ServerTarget`` del servidor del job."""
        return self._server_target(job.server_id)

    def _server_target(self, server_id: int):
        """
        ``ServerTarget`` a partir del id, sin depender de una instancia ORM viva.

        El worker y el ``execute`` cargan el job, cierran la sesión y recién después tocan el
        motor (para no sostener una conexión a la BD del gateway durante una operación
        remota de horas): pasar el id suelto evita que ese patrón dependa de si la instancia
        quedó o no expirada.
        """
        session = self._session()
        try:
            return build_target(get_server_or_404(session, server_id))
        finally:
            session.close()

    @staticmethod
    def _guard_database_live(adapter, database: str) -> None:
        if database not in adapter.list_databases():
            raise AppHttpException(
                message="La base de datos no existe en el servidor.",
                status_code=404,
                context={"database": database},
            )

    @staticmethod
    def _validate_scope(database: str, dialect: str, target) -> None:
        """
        Whitelist ampliada (la BD ya existe, puede tener un nombre legado) + guard de BDs
        de sistema: exportar ``mysql``/``pg_catalog`` no es un caso de uso, y el artefacto
        resultante sería un vector para reescribir el catálogo de otro servidor.

        Y el guard que faltaba: **la propia base de metadatos del gateway**. Si vive en un
        servidor del inventario, nada impedía apuntarle un export — y el artefacto se
        llevaría ``servers`` (con ``root_password_encrypted``), ``server_users``, el
        ``audit_log`` COMPLETO (que el §9.6 declara como el único control compensatorio de
        una exportación sin enmascarado) y ``migration_select_results``. Es exactamente el
        destino que ya bloquea la consola SQL, así que se reusa su guard
        (``query_policy.is_gateway_metadata_target``) en vez de escribir un segundo
        criterio: resuelve ambos hosts a IPs e intersecta, de modo que registrar el
        servidor por su IP en lugar de su nombre no lo evade.
        """
        validate_identifier(database, dialect, "base de datos", allow_existing=True)
        ensure_not_reserved_database(database, dialect)
        # ``target`` es OBLIGATORIO (no tiene default) a propósito: con un default un
        # llamador nuevo se saltearía el guard en silencio, que es justo el modo de fallo
        # que este bloqueante corrige.
        if query_policy.is_gateway_metadata_target(
            host=target.host,
            port=target.port,
            database=database,
            gateway_host=DB_HOST,
            gateway_port=DB_PORT,
            gateway_database=DB_NAME,
        ):
            raise AppHttpException(
                message=(
                    "El destino es la propia base de metadatos del gateway. No se puede "
                    "exportar."
                ),
                status_code=409,
                public_context={"code": espec.CODE_SCOPE_NOT_ALLOWED},
                context={"database": database},
            )

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _spec_dict(spec: espec.ExportSpec) -> dict:
        """
        El spec como dict JSON-serializable y NORMALIZADO (todos los defaults explícitos).

        Se persiste así, y no el cuerpo crudo del request, para que el §3 se cumpla de
        verdad: un job tiene que poder reproducirse aunque un default del código cambie
        entre versiones del gateway.
        """
        return dataclasses.asdict(spec)

    @classmethod
    def _spec_json(cls, spec: espec.ExportSpec) -> str:
        return json.dumps(cls._spec_dict(spec), sort_keys=True, separators=(",", ":"))

    def _serialize_summary(self, job: ExportJob) -> dict:
        spec = json.loads(job.spec) if job.spec else {}
        return {
            "id": job.id,
            "server_id": job.server_id,
            "database_name": job.database_name,
            "database_id": job.database_id,
            "engine": job.engine,
            "format": spec.get("format") or espec.Format.sql.value,
            "status": job.status,
            "phase": job.phase,
            "progress": json.loads(job.progress) if job.progress else None,
            "error": job.error,
            "expired": job.expires_at < _utcnow(),
            "structure_drift_detected": job.structure_drift_detected,
            "has_resolved_selection": job.resolved_selection is not None,
            "idempotency_key": job.idempotency_key,
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    # ------------------------------------------------------------------ #
    # 1) Capacidades (§11.1)                                              #
    # ------------------------------------------------------------------ #
    def capabilities(self, server_id: int, database: str) -> dict:
        """
        Lo que el cliente necesita para construir el formulario sin adivinar.

        Se publica la MISMA matriz que evalúa ``validate_compatibility``
        (``export_spec.compatibility_matrix()``), no una copia: publicar una promesa que el
        servidor no cumple es peor que no publicar nada, y validar con un criterio distinto
        del que se publica es cómo el cliente termina adivinando.

        Los ``default`` NO son literales: salen de instanciar el ``ExportSpec`` real, así
        que lo publicado y lo aplicado no pueden divergir.
        """
        self._guard_enabled()
        dialect, target, _managed_id = self._load_context(server_id, database)
        self._validate_scope(database, dialect, target)

        adapter = get_adapter(target)
        self._guard_database_live(adapter, database)
        info = adapter.test_connection()

        defaults = espec.ExportSpec()
        pg = dialect == "postgresql"

        def _opt(values, default, *, applicable=True, destructive=()) -> dict:
            return {
                "values": [str(v) for v in values],
                "default": str(default) if default is not None else None,
                "applicable": applicable,
                "destructive": list(destructive),
            }

        def _flag(default: bool, *, applicable=True) -> dict:
            """Una opción booleana, publicada con la misma forma que las enumeradas."""
            return {
                "values": ["true", "false"],
                "default": bool(default),
                "applicable": applicable,
                "destructive": [],
            }

        options = {
            "format": _opt(espec.Format, defaults.format),
            "structure.scope_ddl": _opt(
                espec.ScopeDdl,
                defaults.structure.scope_ddl,
                destructive=[espec.ScopeDdl.DROP_CREATE.value],
            ),
            "structure.entity_ddl": _opt(
                espec.EntityDdl,
                defaults.structure.entity_ddl,
                destructive=[espec.EntityDdl.DROP_CREATE.value],
            ),
            "selection.mode": _opt(espec.SelectionMode, defaults.selection.mode),
            "data.mode": _opt(espec.DataSelectionMode, defaults.data.mode),
            "data.insert_variant": _opt(espec.InsertVariant, defaults.data.insert_variant),
            # PostgreSQL: la opción NO es aplicable. El DEFINER de MySQL y la propiedad del
            # objeto (``ALTER … OWNER TO``) / ``SECURITY DEFINER`` de PostgreSQL son
            # mecanismos distintos, así que ahí ``omit``/``replace`` son un 422.
            # Se publica el default YA RESUELTO para ESTE motor (``auto`` → ``omit`` en la
            # familia MySQL, ``keep`` en PostgreSQL): el cliente necesita un valor que pueda
            # mandar tal cual sin comerse un 422, y resolverlo con la misma función que usa
            # el writer (``export_spec.resolve_definer``) es lo que evita que lo publicado y
            # lo aplicado diverjan. ``auto`` sigue en ``values``: es un valor legítimo del
            # spec y el que trae un cuerpo vacío.
            "sanitize.definer": _opt(
                espec.DefinerMode,
                espec.resolve_definer(defaults.sanitize.definer, dialect),
                applicable=not pg,
            ),
            "sanitize.autoincrement": _opt(
                espec.AutoincrementMode, defaults.sanitize.autoincrement
            ),
            "sanitize.constraints_placement": _opt(
                espec.ConstraintsPlacement, defaults.sanitize.constraints_placement
            ),
            "sanitize.charset_override.mode": _opt(
                espec.CharsetOverrideMode, defaults.sanitize.charset_override.mode
            ),
            "output.organization": _opt(espec.Organization, defaults.output.organization),
            "output.compression": _opt(espec.Compression, defaults.output.compression),
            "output.delivery": _opt(espec.Delivery, defaults.output.delivery),
            "output.binary_encoding": _opt(
                espec.BinaryEncoding, defaults.output.binary_encoding
            ),
            "output.schema_manifest": _flag(defaults.output.schema_manifest),
            "csv.line_terminator": _opt(
                espec.LineTerminator, defaults.csv.line_terminator
            ),
            "csv.header": _flag(defaults.csv.header),
            "csv.bom": _flag(defaults.csv.bom),
            "on_error": _opt(espec.OnError, defaults.on_error),
        }

        formats = [
            {"name": "sql", "supports_structure": True, "supports_data": True},
            {
                "name": "csv",
                "supports_structure": False,
                "supports_data": True,
                "one_file_per_table": True,
            },
            {"name": "json", "supports_structure": "manifest_only", "supports_data": True},
            {
                "name": "ndjson",
                "supports_structure": "manifest_only",
                "supports_data": True,
            },
        ]

        family = "postgresql" if pg else "mysql"
        return {
            "engine": dialect,
            "engine_version": info.server_version,
            "scope": {
                "kind": "database",
                "name": database,
                "scope_note": (
                    "PostgreSQL: solo el schema 'public' (misma limitación que el diff, el "
                    "clon y la conversión de collation)."
                    if pg
                    else None
                ),
            },
            "object_types": sorted(adapter.export_supported_types()),
            "formats": formats,
            "options": options,
            "compatibility": espec.compatibility_matrix(),
            "csv_dialect": {
                "delimiter": defaults.csv.delimiter,
                "quote_char": defaults.csv.quote_char,
                "escape_char": defaults.csv.escape_char,
                "null_representation": defaults.csv.null_representation,
                "single_char_options": ["delimiter", "quote_char", "escape_char"],
                "null_vs_empty": (
                    "El NULL se escribe sin comillas como 'null_representation' y la cadena "
                    "vacía SIEMPRE entre comillas: así los dos siguen siendo distinguibles "
                    "al reimportar."
                ),
            },
            # El empaquetado no es una regla de rechazo (por eso no está en la matriz) sino
            # una RESOLUCIÓN del servidor: un artefacto de varios archivos no se puede
            # entregar suelto, así que se eleva a contenedor. Se publica para que el cliente
            # sepa qué extensión y qué tipo de archivo va a recibir antes de lanzar el job.
            "packaging": {
                "multifile_when": [
                    "output.organization=per_object",
                    "output.split_max_bytes",
                ],
                "container": espec.Compression.zip.value,
                "container_is_implicit": True,
                "part_naming": "{base}.part{NN}{ext}",
                "index_entry": "000-INDICE.txt",
                "entry_extension": {
                    fmt.value: export_package.entry_extension(
                        dataclasses.replace(defaults, format=fmt)
                    )
                    for fmt in espec.Format
                },
            },
            "limits": {
                "inline_max_bytes": EXPORT_INLINE_MAX_BYTES,
                "max_statement_bytes": EXPORT_MAX_STATEMENT_BYTES,
                "rows_per_statement": EXPORT_ROWS_PER_STATEMENT,
                "plan_ttl_hours": EXPORT_TTL_HOURS,
                "artifact_ttl_minutes": EXPORT_ARTIFACT_TTL_MINUTES,
                "max_duration_seconds": EXPORT_MAX_DURATION_SECONDS,
                "max_parts": EXPORT_MAX_PARTS,
            },
            "error_codes": sorted(espec.ERROR_CODES),
            "charset_collation_catalog_url": (
                f"/api/v1/charset-collation-options?family={family}"
            ),
        }

    # ------------------------------------------------------------------ #
    # 2) Crear plan                                                       #
    # ------------------------------------------------------------------ #
    def create_plan(
        self,
        server_id: int,
        database: str,
        spec_payload: dict,
        *,
        admin: dict | None = None,
    ) -> dict:
        """
        Crea el PLAN: valida el spec, snapshotea el catálogo y persiste el job ``pending``.

        No toca nada del motor salvo leer: ``list_databases`` y ``structural_snapshot``.
        La validación del spec corre ANTES del snapshot a propósito — si la combinación de
        opciones es imposible, no tiene sentido gastarle una lectura de catálogo al
        servidor de un tercero.
        """
        self._guard_enabled()
        dialect, target, managed_id = self._load_context(server_id, database)
        self._validate_scope(database, dialect, target)

        spec = espec.ExportSpec.from_dict(spec_payload)
        # Matriz completa contra el motor REAL del servidor: las reglas que dependen del
        # motor (DEFINER en PostgreSQL, DROP DATABASE transaccional) solo se pueden evaluar
        # acá, no en el borde HTTP.
        espec.raise_for_incompatibilities(
            espec.validate_compatibility(spec, engine=dialect)
        )
        self._guard_scope_drop_confirmation(spec, database)
        # La plantilla del nombre se valida ahora y no en la descarga: un token inválido
        # descubierto recién al final desperdiciaría la exportación entera.
        espec.sanitize_filename_template(
            spec.output.filename_template,
            {"database": database, "date": "", "time": "", "job_id": "", "object": ""},
        )

        spec_json = self._spec_json(spec)
        replay = self._idempotent_replay(spec.idempotency_key, spec_json)
        if replay is not None:
            return replay

        adapter = get_adapter(target)
        self._guard_database_live(adapter, database)
        snapshot = adapter.structural_snapshot(database)
        fingerprint = _snapshot_fingerprint(snapshot)

        session = self._session()
        try:
            job = ExportJob(
                server_id=server_id,
                database_name=database,
                database_id=managed_id,
                engine=dialect,
                spec=spec_json,
                source_fingerprint=fingerprint,
                expires_at=_utcnow() + timedelta(hours=EXPORT_TTL_HOURS),
                status=EXPORT_STATUS_PENDING,
                created_by_admin_id=(admin or {}).get("id"),
                idempotency_key=spec.idempotency_key,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            result = self._serialize_summary(job)
            job_id = job.id
        finally:
            session.close()

        # ``touched_engine=True`` (§9.4 corregido): el flag significa "esta operación
        # CONTACTÓ el motor", no "lo mutó" — y el plan snapshotea la estructura en vivo
        # (``list_databases`` + ``structural_snapshot``). Por eso ``clone.plan`` también usa
        # ``True``, mientras que el ``export`` de schema-comparisons usa ``False``: ese
        # último solo lee ítems ya persistidos y nunca abre una conexión al servidor.
        audit.record(
            "database_export.plan",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"plan de exportación {job_id}: {server_id}/{database} "
                f"(formato={spec.format}, estructura={spec.selection.mode}, "
                f"datos={spec.data.mode})"
            ),
        )
        return result

    def _idempotent_replay(self, key: str | None, spec_json: str) -> dict | None:
        """
        Reintento del cliente: misma clave + mismo spec ⇒ el MISMO plan, no uno nuevo.

        Una exportación es cara para el servidor de origen (sostiene una transacción de
        lectura larga), así que un timeout de red o un doble click no pueden multiplicarla.
        Con la misma clave y un spec DISTINTO es un 409: reutilizar una clave para otra cosa
        es un bug del cliente, y responder el plan viejo lo escondería.
        """
        if not key:
            return None
        session = self._session()
        try:
            existing = (
                session.query(ExportJob)
                .filter(ExportJob.idempotency_key == key)
                .one_or_none()
            )
            if existing is None:
                return None
            if existing.spec != spec_json:
                raise AppHttpException(
                    message=(
                        "Esa clave de idempotencia ya se usó con otras opciones de "
                        "exportación."
                    ),
                    status_code=409,
                    public_context={
                        "code": "export.idempotency_conflict",
                        "field": "idempotency_key",
                        "export_job_id": existing.id,
                    },
                )
            return self._serialize_summary(existing)
        finally:
            session.close()

    @staticmethod
    def _guard_scope_drop_confirmation(spec: espec.ExportSpec, database: str) -> None:
        """
        ``DROP_CREATE`` del contenedor exige re-teclear el nombre REAL de la base.

        La matriz ya exige que el campo esté PRESENTE; comparar el valor es del controller,
        que es el único que conoce el nombre. Es el mismo patrón ``confirm_target_name`` del
        clon y del borrado de BDs: obliga a identificar CUÁL base, no solo a confirmar.
        """
        if spec.structure.scope_ddl != espec.ScopeDdl.DROP_CREATE:
            return
        if (spec.structure.confirm_scope_drop or "") != database:
            raise AppHttpException(
                message=(
                    "confirm_scope_drop no coincide con el nombre de la base de datos: el "
                    "artefacto incluiría un DROP DATABASE."
                ),
                status_code=422,
                public_context={
                    "code": espec.CODE_INCOMPATIBLE_OPTION,
                    "field": "structure.confirm_scope_drop",
                },
            )

    # ------------------------------------------------------------------ #
    # 3) Catálogo (§2.3.2)                                                #
    # ------------------------------------------------------------------ #
    def list_objects(
        self,
        job_id: int,
        *,
        object_type: str | None = None,
        name_like: str | None = None,
        limit: int,
        offset: int,
    ) -> dict:
        """
        Catálogo EN VIVO de la BD, filtrable y paginado, con los metadatos que informan la
        selección.

        Los filtros se aplican EN MEMORIA sobre los nombres que devolvió el motor: ni
        ``object_type`` ni ``name_like`` llegan nunca a una consulta.
        """
        self._guard_enabled()
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            database = job.database_name
            engine = job.engine
            spec = espec.ExportSpec.from_dict(json.loads(job.spec))
        finally:
            session.close()

        target = self._job_target(job)
        adapter = get_adapter(target)
        snapshot = adapter.structural_snapshot(database)
        # Estimaciones y PK vienen del catálogo del motor, no de un COUNT: contar de verdad
        # recorrería cada tabla del origen, que es justo lo que un catálogo no debe hacer.
        stats = {s.table: s for s in adapter.list_table_stats(database)}

        tables_by_name = {t.table: t for t in snapshot.tables}
        tables_with_triggers = {tg.table for tg in snapshot.triggers}
        filtered_names = set(spec.data.per_object)

        rows: list[dict] = []
        excluded_internal: list[str] = []
        counts: dict[str, int] = {}
        for otype, name in _iter_catalog(snapshot):
            # Obligatorio en TODO camino que enumera tablas: la contabilidad interna del
            # gateway (``_gw_v_*``/``_gw_stg_*``) no es esquema del usuario, y el incidente
            # de producción de 2026-07-27 nació justo de un camino que no la excluía.
            if otype == "table" and is_gateway_internal_table(name):
                excluded_internal.append(name)
                continue
            counts[otype] = counts.get(otype, 0) + 1
            table = tables_by_name.get(name) if otype == "table" else None
            stat = stats.get(name) if otype == "table" else None
            storage = table.storage_options if table else {}
            rows.append(
                {
                    "object_type": otype,
                    "name": name,
                    "estimated_rows": stat.estimated_rows if stat else None,
                    # Sin fuente: ningún método del adapter expone el tamaño en disco. Se
                    # deja el campo (contrato estable para el cliente) en vez de inventar
                    # una cifra que parecería medida.
                    "size_bytes": None,
                    "charset": storage.get("charset"),
                    "collation": storage.get("collation"),
                    "has_primary_key": (
                        stat.has_primary_key
                        if stat
                        else (bool(table.primary_key) if table else None)
                    ),
                    "has_triggers": (name in tables_with_triggers) if table else None,
                    "is_materialized": (otype == "materialized_view") or None,
                    "row_filter": name in filtered_names,
                }
            )

        if object_type:
            rows = [r for r in rows if r["object_type"] == object_type]
        if name_like:
            needle = name_like.lower()
            rows = [r for r in rows if needle in r["name"].lower()]

        total = len(rows)
        page = (offset // limit) + 1 if limit else 1
        return {
            "engine": engine,
            "database": database,
            "scope_note": _scope_note(engine),
            "object_types": sorted(adapter.export_supported_types()),
            "counts_by_type": counts,
            "objects": rows[offset : offset + limit],
            "total": total,
            "page": page,
            "size": limit,
            "excluded_internal": excluded_internal,
        }

    # ------------------------------------------------------------------ #
    # 4) Resolver selección (§5.3 / §5.4)                                 #
    # ------------------------------------------------------------------ #
    def resolve_selection(
        self,
        job_id: int,
        *,
        selection: dict | None = None,
        data: dict | None = None,
        auto_resolve_dependencies: bool = False,
    ) -> dict:
        """
        Resuelve las dos selecciones y su cierre de dependencias, **sin congelar nada**.

        Es el endpoint que alimenta el auto-select de la interfaz: devuelve exactamente lo
        que el preview congelaría, para que el usuario lo vea antes de comprometerse.
        """
        self._guard_enabled()
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            spec = espec.ExportSpec.from_dict(json.loads(job.spec))
            database = job.database_name
        finally:
            session.close()

        spec = self._with_selection_overrides(spec, selection, data)
        snapshot = get_adapter(self._job_target(job)).structural_snapshot(database)
        resolved, _ = self._resolve(
            snapshot, spec, auto_resolve_dependencies=auto_resolve_dependencies
        )
        return resolved

    @staticmethod
    def _with_selection_overrides(
        spec: espec.ExportSpec, selection: dict | None, data: dict | None
    ) -> espec.ExportSpec:
        """Reemplaza selección/datos del spec sin mutar el resto (dataclass frozen)."""
        payload = ExportController._spec_dict(spec)
        if selection is not None:
            payload["selection"] = selection
        if data is not None:
            payload["data"] = data
        return espec.ExportSpec.from_dict(payload)

    def _resolve(
        self,
        snapshot: SchemaSnapshot,
        spec: espec.ExportSpec,
        *,
        auto_resolve_dependencies: bool,
    ) -> tuple[dict, espec.ResolvedSelection]:
        """
        Núcleo compartido por ``resolve_selection`` y ``preview``.

        Orden de los tres controles, que importa:

        1. resolver ambas selecciones contra el catálogo (``export_spec.resolve_selection``);
        2. **datos ⊆ estructura** (§5.3), con la excepción "solo datos" ya codificada en
           ``check_data_subset``. Va antes del cierre porque un conjunto de datos incoherente
           no se arregla agregando dependencias;
        3. **cierre de dependencias** (§5.4) con la política del proyecto: una selección
           EXPLÍCITA (``include``) no se recorta en silencio —es 422 con las dependencias que
           faltan y la selección sugerida— salvo ``auto_resolve_dependencies``; una selección
           AUTOMÁTICA (``all``/``all_except``/patrones) se PODA transitivamente y lo excluido
           viaja en la respuesta. La asimetría es deliberada: en el primer caso el usuario
           enumeró objetos y quitarle uno sería contradecirlo; en el segundo describió un
           criterio y el cierre es parte de aplicarlo bien.
        """
        catalog = _catalog_objects(snapshot)
        structure = espec.resolve_selection(catalog, spec.selection)
        data_sel = espec.resolve_selection(catalog, spec.data.selection)

        orphan_data = espec.check_data_subset(structure, data_sel, spec)
        if orphan_data:
            raise AppHttpException(
                message=(
                    "Hay tablas seleccionadas para datos que no están en la selección de "
                    "estructura: el artefacto tendría INSERTs sin la tabla que los recibe."
                ),
                status_code=422,
                public_context={
                    "code": espec.CODE_DATA_WITHOUT_STRUCTURE,
                    "field": "data.names",
                    "data_without_structure": sorted(orphan_data),
                },
            )

        warnings: list[str] = []
        if structure.unknown_names:
            warnings.append(
                "Nombres pedidos que el catálogo no tiene: "
                + ", ".join(sorted(structure.unknown_names)[:10])
            )

        refs = [
            cdeps.ObjectRef(object_type=o.object_type, name=o.name)
            for o in structure.objects
        ]
        closure = cdeps.resolve_closure(snapshot, refs)
        selected_keys = {(o.object_type, o.name) for o in structure.objects}
        missing = [
            (r.object_type, r.name)
            for r in closure.added
            if (r.object_type, r.name) not in selected_keys
        ]

        added: list[dict] = []
        excluded: list[dict] = []
        explicit = spec.selection.mode == espec.SelectionMode.include

        if missing and explicit and not auto_resolve_dependencies:
            suggested = sorted({name for _t, name in missing} | set(structure.names))
            raise AppHttpException(
                message=(
                    "La selección deja fuera objetos de los que depende: el artefacto no "
                    "se podría ejecutar."
                ),
                status_code=422,
                public_context={
                    "code": espec.CODE_MISSING_DEPENDENCIES,
                    "field": "selection.names",
                    "missing_dependencies": [
                        {"object_type": t, "name": n} for t, n in sorted(missing)
                    ],
                    "suggested_names": suggested,
                },
            )

        if missing and (explicit and auto_resolve_dependencies):
            for otype, name in sorted(missing):
                selected_keys.add((otype, name))
                added.append({"object_type": otype, "name": name})
        elif missing:
            # Selección automática: se PODA. El caso testigo es el filtro por riesgo que
            # dejaba fuera una tabla pero no sus índices, y el artefacto quedaba con un
            # CREATE INDEX sobre una tabla inexistente.
            kept, pruned = _prune_unsatisfied(snapshot, selected_keys)
            selected_keys = kept
            excluded = [{"object_type": t, "name": n} for t, n in sorted(pruned)]

        # La lista final conserva el ORDEN DEL CATÁLOGO (los objetos ya seleccionados
        # primero, en su orden original) y agrega al final los que trajo el cierre. El orden
        # definitivo lo fija ``_order_for_emission``; acá solo importa que sea determinista.
        original = [(o.object_type, o.name) for o in structure.objects]
        final_structure = [
            o for o in structure.objects if (o.object_type, o.name) in selected_keys
        ]
        final_structure += [
            espec.CatalogObject(object_type=t, name=n)
            for t, n in sorted(selected_keys - set(original))
        ]

        final = espec.ResolvedSelection(
            objects=tuple(final_structure),
            excluded_internal=structure.excluded_internal,
            unknown_names=structure.unknown_names,
        )
        # Los datos de una tabla podada dejan de tener sentido... salvo en el modo "solo
        # datos" (§5.3), donde no hay estructura contra la cual recortar: ahí la selección
        # de datos es autónoma por diseño.
        final_data = (
            [o.name for o in data_sel.objects]
            if _data_only(spec)
            else [
                o.name
                for o in data_sel.objects
                if (o.object_type, o.name) in selected_keys
            ]
        )

        payload = {
            "structure": [
                {"object_type": o.object_type, "name": o.name} for o in final.objects
            ],
            "data": final_data,
            "added": added,
            "excluded_by_dependency": excluded,
            "edges": [e.model_dump() for e in closure.edges],
            "advisory": [e.model_dump() for e in closure.advisory],
            "excluded_internal": list(final.excluded_internal),
            "unknown_names": list(final.unknown_names),
            "warnings": warnings + list(closure.warnings),
        }
        return payload, final

    # ------------------------------------------------------------------ #
    # 5) Preview (§2.3.3)                                                 #
    # ------------------------------------------------------------------ #
    def preview(
        self,
        job_id: int,
        *,
        spec_payload: dict | None = None,
        auto_resolve_dependencies: bool = False,
        dry_run_only: bool = False,
        include_sample: bool = False,
    ) -> dict:
        """
        Valida el spec entero, CONGELA la selección y emite el ``confirm_token``.

        El preview es el punto de congelación del §5.2: el catálogo se relee, los patrones
        se resuelven a una lista explícita de objetos y el token hashea ESA lista. Un objeto
        creado entre el preview y el execute no entra, y el ``source_fingerprint`` se
        actualiza acá porque este —y no la creación del plan— es el instante que el execute
        va a comparar.

        ``dry_run_only`` es el modo "solo advertencias": valida y reporta sin congelar nada
        ni emitir token, para que el formulario muestre las consecuencias mientras el
        usuario todavía está eligiendo.

        Solo se admite sobre un job ``pending``. Sobre uno que YA se ejecutó, el preview
        sobrescribía ``spec``/``resolved_selection``/``fingerprint``/``token``: el
        ``GET /manifest`` pasaba a describir una selección que no es la del artefacto
        entregado, y con ella el registro de qué se llevó cada exportación deja de ser
        confiable. Un plan nuevo se crea con ``POST /database-exports``, que es barato.
        """
        self._guard_enabled()
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            self._guard_still_pending(job, "previsualizar de nuevo")
            database = job.database_name
            engine = job.engine
            stored_spec = json.loads(job.spec)
        finally:
            session.close()

        spec = espec.ExportSpec.from_dict(spec_payload if spec_payload is not None else stored_spec)
        incompatibilities = espec.validate_compatibility(spec, engine=engine)
        espec.raise_for_incompatibilities(incompatibilities)
        self._guard_scope_drop_confirmation(spec, database)
        espec.sanitize_filename_template(
            spec.output.filename_template,
            {
                "database": database,
                "date": "",
                "time": "",
                "job_id": str(job_id),
                "object": "",
            },
        )

        adapter = get_adapter(self._job_target(job))
        snapshot = adapter.structural_snapshot(database)
        closure_payload, resolved = self._resolve(
            snapshot, spec, auto_resolve_dependencies=auto_resolve_dependencies
        )

        # Los filtros de filas se validan con la maquinaria que ya existe (query_policy),
        # contra las columnas REALES de la tabla, y ANTES de tocar el motor.
        warnings = list(closure_payload["warnings"])
        warnings += self._validate_row_filters(
            spec, snapshot, closure_payload["data"], engine, adapter
        )

        ordered, order_warnings = _order_for_emission(snapshot, list(resolved.objects))
        warnings += order_warnings

        data_names = set(closure_payload["data"])
        stats = {s.table: s for s in adapter.list_table_stats(database)}
        tables_by_name = {t.table: t for t in snapshot.tables}

        objects: list[dict] = []
        estimated_rows = 0
        estimated_bytes = 0
        no_pk: list[str] = []
        for seq, (step, obj) in enumerate(ordered, start=1):
            with_data = obj.object_type == "table" and obj.name in data_names
            stat = stats.get(obj.name)
            table = tables_by_name.get(obj.name)
            rows = None
            deterministic = True
            if with_data:
                limit = (spec.data.per_object.get(obj.name) or espec.RowFilter()).limit
                rows = stat.estimated_rows if stat else 0
                if limit is not None:
                    rows = min(rows, limit)
                estimated_rows += rows
                if table is not None:
                    estimated_bytes += rows * _nominal_row_bytes(table)
                # Sin PK y sin tupla ordenable no hay orden garantizado: el artefacto deja
                # de ser comparable byte a byte (§8.3). Se marca, no se disimula.
                if stat is not None and not stat.has_primary_key:
                    deterministic = False
                    no_pk.append(obj.name)
            estimated_bytes += _DDL_NOMINAL_BYTES
            objects.append(
                {
                    "seq": seq,
                    "object_type": obj.object_type,
                    "name": obj.name,
                    "phase": _PHASE_BY_TYPE.get(obj.object_type, EXPORT_PHASE_STRUCTURE),
                    "step": step,
                    "with_data": with_data,
                    "estimated_rows": rows,
                    "deterministic": deterministic,
                }
            )

        warnings += self._consistency_warnings(engine, spec, no_pk)
        warnings += self._packaging_warnings(spec, len(data_names))
        if include_sample:
            warnings.append(
                "La muestra del artefacto todavía no está disponible: el generador se "
                "incorpora en una fase posterior."
            )

        inline = spec.output.delivery == espec.Delivery.inline
        inline_viable = (not inline) or estimated_bytes <= EXPORT_INLINE_MAX_BYTES
        if inline and not inline_viable:
            # No es un error: es información accionable ANTES de lanzar el job. El 409 de
            # ``export.inline_too_large`` corresponde al momento de la entrega, no acá.
            warnings.append(
                f"La entrega en línea admite hasta {EXPORT_INLINE_MAX_BYTES} bytes y la "
                f"estimación es de {estimated_bytes}: usá output.delivery='file'."
            )

        token = None
        if not dry_run_only:
            token = self.export_execution_token(
                server_ref=f"{job.server_id}:{database}",
                engine=engine,
                spec=spec,
                objects=[(o["object_type"], o["name"]) for o in objects],
                data_tables=sorted(data_names),
            )
            session = self._session()
            try:
                job = self._job_or_404(session, job_id)
                job.spec = self._spec_json(spec)
                job.resolved_selection = json.dumps(
                    {
                        "objects": [
                            {"object_type": o["object_type"], "name": o["name"]}
                            for o in objects
                        ],
                        "data": sorted(data_names),
                        # La estimación viaja con la selección congelada porque el
                        # ``execute`` la necesita para comprobar el espacio libre del disco
                        # (§9.7) y no puede recalcularla sin releer el catálogo. NO entra en
                        # el ``confirm_token``: es un dato derivado y aproximado, no parte
                        # del plan que el operador confirma.
                        "estimated_bytes": estimated_bytes,
                    },
                    separators=(",", ":"),
                )
                job.source_fingerprint = _snapshot_fingerprint(snapshot)
                job.confirm_token = token
                session.commit()
            finally:
                session.close()

        return {
            "job_id": job_id,
            "engine": engine,
            "database": database,
            "format": str(spec.format),
            "scope_note": _scope_note(engine),
            "objects": objects,
            "data_tables": sorted(data_names),
            "estimated_rows": estimated_rows,
            "estimated_bytes": estimated_bytes,
            "inline_delivery_viable": inline_viable,
            "inline_max_bytes": EXPORT_INLINE_MAX_BYTES,
            "warnings": warnings,
            "advisories": [
                {
                    "when": dict(i.detail),
                    "forbids": [],
                    "requires": [],
                    "reason": i.message,
                    "blocking": False,
                    "code": i.code,
                }
                for i in incompatibilities
                if not i.blocking
            ],
            "excluded_by_dependency": closure_payload["excluded_by_dependency"],
            "sample": None,
            "confirm_token": token,
        }

    @staticmethod
    def _validate_row_filters(
        spec: espec.ExportSpec,
        snapshot: SchemaSnapshot,
        data_tables: list[str],
        engine: str,
        adapter=None,
    ) -> list[str]:
        """
        Valida cada ``where`` contra las columnas reales de SU tabla. 422 si no pasa.

        Se valida la consulta COMPLETA, con el mismo ``ORDER BY`` y el mismo ``LIMIT`` con
        los que el writer la va a ejecutar: validar solo el prefijo dejaba que un ``where``
        terminado en comentario comentara la cola de la sentencia. Las columnas son las
        INSERTABLES (sin las generadas), que son las que efectivamente viajan en el
        ``SELECT`` — ver ``export_writer._insertable_columns``.
        """
        warnings: list[str] = []
        tables_by_name = {t.table: t for t in snapshot.tables}
        exported = set(data_tables)
        for name, row_filter in spec.data.per_object.items():
            if name not in exported:
                warnings.append(
                    f"Hay un filtro de filas para '{name}', que no está en la selección de "
                    "datos: no se va a aplicar."
                )
                continue
            if not row_filter.where:
                continue
            table = tables_by_name.get(name)
            columns = (
                [c.name for c in table.columns if c.computed is None] if table else []
            )
            order_by = (
                adapter.export_row_order_by(table)
                if adapter is not None and table is not None
                else ()
            )
            espec.validate_row_filter(
                row_filter.where,
                name,
                columns,
                engine,
                order_by=order_by,
                limit=row_filter.limit,
            )
        return warnings

    @staticmethod
    def _consistency_warnings(
        engine: str, spec: espec.ExportSpec, no_pk: list[str]
    ) -> list[str]:
        """Los avisos que el diseño exige emitir y NO tapar."""
        out: list[str] = []
        exports_structure = (
            spec.structure.entity_ddl != espec.EntityDdl.NONE
            or spec.structure.scope_ddl != espec.ScopeDdl.NONE
        )
        if engine in _MYSQL_FAMILY and exports_structure:
            # §6.2: el snapshot consistente de InnoDB es MVCC de FILAS; el diccionario de
            # datos y information_schema no participan. Congelar también el catálogo exigiría
            # FLUSH TABLES WITH READ LOCK, que bloquea escrituras en el servidor ENTERO —
            # inaceptable en un gateway que administra bases de terceros. Es la misma
            # limitación de mysqldump --single-transaction, y se reporta en vez de taparse.
            out.append(
                "En MySQL/MariaDB la consistencia de punto único cubre los DATOS pero no la "
                "ESTRUCTURA: un ALTER TABLE concurrente se ve de inmediato. Los datos siguen "
                "siendo consistentes; la estructura puede reflejar un instante posterior."
            )
        if engine == "postgresql" and spec.structure.scope_ddl == espec.ScopeDdl.DROP_CREATE:
            out.append(
                "El DROP DATABASE del artefacto no es ejecutable desde una conexión a esa "
                "misma base ni dentro de un bloque transaccional: quien lo ejecute debe "
                "conectarse a otra (por ejemplo 'postgres')."
            )
        if no_pk:
            out.append(
                "Tablas sin clave primaria seleccionadas para datos ("
                + ", ".join(sorted(no_pk)[:5])
                + "): sus filas salen sin orden garantizado y el artefacto deja de ser "
                "comparable byte a byte."
            )
        return out

    @staticmethod
    def _packaging_warnings(spec: espec.ExportSpec, data_count: int) -> list[str]:
        """
        Consecuencias del formato y del empaquetado que el operador tiene que ver ANTES.

        Ninguna es un rechazo: son cosas que el artefacto va a hacer y que no se deducen de
        las opciones a simple vista. Un artefacto vacío o un ``.zip`` donde se esperaba un
        ``.sql`` no son errores, pero descubrirlos después de una lectura completa del origen
        sí es un problema evitable.
        """
        out: list[str] = []
        if spec.format != espec.Format.sql and data_count == 0:
            out.append(
                f"El formato '{spec.format}' solo transporta datos y la selección de datos "
                "quedó vacía: el artefacto no va a llevar ninguna fila."
            )
        if (
            espec.is_multifile(spec)
            and spec.output.compression == espec.Compression.none
        ):
            out.append(
                "El artefacto tiene varios archivos, así que se entrega comprimido en zip "
                f"({espec.effective_compression(spec)}): un multiarchivo no se puede "
                "entregar suelto por una descarga."
            )
        if spec.csv.bom and spec.output.file_encoding.lower().replace("_", "-") in (
            "utf-8-sig",
            "utf8-sig",
        ):
            out.append(
                "csv.bom junto con file_encoding='utf-8-sig' escribe la marca de orden de "
                "bytes DOS veces: dejá una sola de las dos."
            )
        return out

    # ------------------------------------------------------------------ #
    # Token de confirmación                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def export_execution_token(
        *,
        server_ref: str,
        engine: str,
        spec: espec.ExportSpec,
        objects: list[tuple[str, str]],
        data_tables: list[str],
    ) -> str:
        """
        SHA256 del PLAN RESUELTO (destino + spec normalizado + objetos en orden + datos).

        Es el mismo criterio que ``clone_controller.clone_execution_token`` y NO el HMAC
        stateless de ``confirm_token.py``: acá el token no tiene que probar frescura contra
        un reloj, tiene que probar que lo que se confirma es EXACTAMENTE lo que se
        previsualizó. Si cambia un objeto, su orden o una sola opción del spec, el hash
        cambia y el execute rechaza.
        """
        parts: list[str] = [server_ref, engine, ExportController._spec_json(spec)]
        parts += [f"obj:{t}:{n}" for t, n in objects]
        parts += [f"data:{n}" for n in data_tables]
        blob = "\x1f".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # 6) Execute — valida y ENCOLA (no genera nada en el hilo de la API)  #
    # ------------------------------------------------------------------ #
    def execute(
        self,
        job_id: int,
        *,
        confirm_target_name: str,
        confirm_token: str,
        admin: dict | None = None,
    ) -> dict:
        """
        Confirma el plan congelado y encola la generación del artefacto.

        Los cinco controles corren EN ESTE ORDEN, que es el del resto del proyecto y no es
        arbitrario: primero lo barato y local (TTL, estado, nombre re-tecleado), después lo
        que exige tocar el motor (re-snapshot anti-TOCTOU) y recién al final el token, que
        solo tiene sentido comparar contra el catálogo actual. El worker vuelve a comprobar
        el fingerprint (cuarta comprobación): entre este instante y el arranque real del job
        puede pasar tiempo de cola.

        La auditoría de intención va **fail-closed y antes de encolar**: si no se persiste,
        no se lanza nada. Una exportación es una extracción masiva de datos en claro y el
        rastro tiene que existir aunque el proceso muera un segundo después.
        """
        self._guard_enabled()
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            self._guard_still_pending(job, "re-ejecutar")
            if confirm_target_name != job.database_name:
                raise AppHttpException(
                    message="confirm_target_name no coincide con el nombre de la base de datos.",
                    status_code=422,
                    public_context={
                        "code": espec.CODE_INCOMPATIBLE_OPTION,
                        "field": "confirm_target_name",
                    },
                )
            if not job.resolved_selection or not job.confirm_token:
                raise AppHttpException(
                    message=(
                        "El plan todavía no se previsualizó: no hay selección congelada "
                        "que confirmar."
                    ),
                    status_code=409,
                    public_context={"code": "export.not_previewed"},
                )
            spec = espec.ExportSpec.from_dict(json.loads(job.spec))
            resolved = json.loads(job.resolved_selection)
            database = job.database_name
            engine = job.engine
            server_id = job.server_id
            managed_id = job.database_id
            fingerprint = job.source_fingerprint
        finally:
            session.close()

        self._guard_concurrency(session_factory=self._session)

        # Anti-TOCTOU: el catálogo se relee AHORA y el token se recalcula contra la selección
        # CONGELADA (no contra una resolución nueva). Si el catálogo cambió, el fingerprint
        # ya no coincide y hay que volver a previsualizar; si coincide, la selección
        # congelada sigue describiendo objetos que existen.
        adapter = get_adapter(self._server_target(server_id))
        snapshot = adapter.structural_snapshot(database)
        if _snapshot_fingerprint(snapshot) != fingerprint:
            raise AppHttpException(
                message=(
                    "El esquema del origen cambió desde la previsualización; volvé a "
                    "previsualizar."
                ),
                status_code=409,
                public_context={
                    "code": espec.CODE_FINGERPRINT_CHANGED,
                    "field": "confirm_token",
                },
            )

        expected = self.export_execution_token(
            server_ref=f"{server_id}:{database}",
            engine=engine,
            spec=spec,
            objects=[(o["object_type"], o["name"]) for o in resolved["objects"]],
            data_tables=list(resolved.get("data") or []),
        )
        if confirm_token != expected:
            raise AppHttpException(
                message=(
                    "confirm_token no coincide con el plan actual; volvé a previsualizar."
                ),
                status_code=422,
                public_context={
                    "code": espec.CODE_INCOMPATIBLE_OPTION,
                    "field": "confirm_token",
                },
            )

        audit.record_intent(
            "database_export.execute",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"INTENT exportar {server_id}/{database} (job {job_id}, "
                f"objetos={len(resolved['objects'])}, "
                f"tablas con datos={len(resolved.get('data') or [])})"
            ),
        )

        from app.services import export_runner

        export_runner.enqueue(job_id)
        return self.get_job(job_id)

    @staticmethod
    def _guard_concurrency(*, session_factory) -> None:
        """
        Techo de exportaciones simultáneas (§9.7).

        Con un solo worker la cola ya serializa, pero sin este tope un cliente puede encolar
        cientos de jobs: el origen queda leyéndose durante días y el disco del gateway se
        llena de artefactos. Es también la única defensa barata contra una exfiltración
        lenta hecha a fuerza de jobs.

        Se cuentan las EN COLA además de las EN CURSO. Con solo ``running``, y un pool de un
        worker, el techo era siempre 1 y la COLA quedaba sin acotar: se podían admitir miles
        de jobs que el worker iba a ejecutar igual, uno tras otro. El tope tiene que
        gobernar el trabajo ADMITIDO, no cuántos corren en este instante.

        La cola se cuenta con ``export_runner.inflight_count()`` y no con un ``SELECT`` de
        ``pending`` en la base: un job encolado sigue ``pending`` hasta que el worker lo
        reclama, pero ``pending`` incluye también los PLANES creados y nunca ejecutados —que
        no son trabajo admitido— y contarlos habría bloqueado el endpoint por planes viejos
        que nadie va a lanzar. Se toma el máximo con el conteo de ``running`` de la base para
        no perder los jobs que un reinicio dejó corriendo y que este proceso no encoló.
        """
        if EXPORT_MAX_CONCURRENT_GLOBAL <= 0:
            return
        session = session_factory()
        try:
            db_running = (
                session.query(ExportJob)
                .filter(ExportJob.status == EXPORT_STATUS_RUNNING)
                .count()
            )
        finally:
            session.close()

        from app.services import export_runner

        running = max(db_running, export_runner.inflight_count())
        if running >= EXPORT_MAX_CONCURRENT_GLOBAL:
            raise AppHttpException(
                message=(
                    f"Ya hay {running} exportación(es) en curso o en cola (máximo "
                    f"{EXPORT_MAX_CONCURRENT_GLOBAL}). Esperá a que terminen."
                ),
                status_code=409,
                public_context={
                    "code": espec.CODE_QUOTA_EXCEEDED,
                    "running": running,
                    "limit": EXPORT_MAX_CONCURRENT_GLOBAL,
                },
            )

    # ------------------------------------------------------------------ #
    # 7-9) Polling, ítems y cancelación                                   #
    # ------------------------------------------------------------------ #
    def get_job(self, job_id: int) -> dict:
        """Cabecera + estado del job (lo que consume el polling)."""
        session = self._session()
        try:
            return self._serialize_summary(self._job_or_404(session, job_id))
        finally:
            session.close()

    def list_items(self, job_id: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
        """Reporte de incidencias por objeto (§14), paginado y en orden de emisión."""
        session = self._session()
        try:
            self._job_or_404(session, job_id)
            query = session.query(ExportJobItem).filter(ExportJobItem.job_id == job_id)
            total = query.count()
            rows = (
                query.order_by(ExportJobItem.seq.asc()).limit(limit).offset(offset).all()
            )
            return [self._serialize_item(r) for r in rows], total
        finally:
            session.close()

    @staticmethod
    def _serialize_item(row: ExportJobItem) -> dict:
        return {
            "id": row.id,
            "job_id": row.job_id,
            "seq": row.seq,
            "object_type": row.object_type,
            "object_name": row.object_name,
            "phase": row.phase,
            "status": row.status,
            "reason": row.reason,
            "rows_exported": row.rows_exported,
            "bytes_written": row.bytes_written,
            "deterministic": row.deterministic,
            "execution_ms": row.execution_ms,
            "executed_at": row.executed_at,
        }

    def cancel(self, job_id: int, *, admin: dict | None = None) -> dict:
        """
        Solicita la cancelación COOPERATIVA (el worker corta en el próximo punto seguro).

        No pasa por el kill switch a propósito: si alguien apaga la exportación con un job
        en curso, lo que necesita es poder DETENERLO. Bloquear la cancelación sería lo
        contrario de lo que el switch persigue.
        """
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            if job.status not in (EXPORT_STATUS_PENDING, EXPORT_STATUS_RUNNING):
                raise AppHttpException(
                    message=f"El job no se puede cancelar en estado '{job.status}'.",
                    status_code=409,
                    public_context={"code": "export.not_cancellable", "status": job.status},
                )
            job.cancel_requested = True
            session.commit()
            session.refresh(job)
            result = self._serialize_summary(job)
        finally:
            session.close()
        audit.record(
            "database_export.cancel",
            admin=admin,
            touched_engine=False,
            detail=f"cancelación solicitada para la exportación {job_id}",
        )
        return result

    def sweep_interrupted(self) -> int:
        """Marca ``running → interrupted`` (barrido de arranque tras un reinicio)."""
        session = self._session()
        try:
            rows = (
                session.query(ExportJob)
                .filter(ExportJob.status == EXPORT_STATUS_RUNNING)
                .all()
            )
            for job in rows:
                job.status = EXPORT_STATUS_INTERRUPTED
                job.finished_at = _utcnow()
                job.error = "El proceso se reinició mientras el job estaba en ejecución."
            session.commit()
            return len(rows)
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Estado del worker                                                   #
    # ------------------------------------------------------------------ #
    def _set_status(self, job_id, status, *, phase=None, error=None, finished=False,
                    drift=None):
        session = self._session()
        try:
            job = session.get(ExportJob, job_id)
            if job is None:
                return
            job.status = status
            if phase is not None:
                job.phase = phase
            if error is not None:
                job.error = error
            if drift is not None:
                job.structure_drift_detected = bool(drift)
            if finished:
                job.finished_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def _set_progress(self, job_id, progress: dict):
        session = self._session()
        try:
            job = session.get(ExportJob, job_id)
            if job is not None:
                job.progress = json.dumps(progress, separators=(",", ":"))
                session.commit()
        finally:
            session.close()

    def _record_items(self, job_id: int, items) -> None:
        """Vuelca el reporte por objeto del writer a ``export_job_items``."""
        rows = list(items)
        if not rows:
            return
        now = _utcnow()
        session = self._session()
        try:
            for item in rows:
                session.add(
                    ExportJobItem(
                        job_id=job_id,
                        seq=item.seq,
                        object_type=item.object_type,
                        object_name=item.object_name,
                        phase=item.phase,
                        status=item.status,
                        # ``reason`` ya viene de un vocabulario CERRADO del writer
                        # (``unsupported_type:…``, ``no_ddl_rendered``): nunca el mensaje de
                        # un driver, que puede incrustar valores de filas (R4, §9.5).
                        reason=item.reason,
                        rows_exported=item.rows_exported,
                        bytes_written=item.bytes_written,
                        deterministic=item.deterministic,
                        executed_at=now,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _cancel_checker(self, job_id: int):
        """Callable que lee ``cancel_requested`` de la BD, cacheado 2 s para no martillar."""
        state = {"val": False, "ts": 0.0}

        def check() -> bool:
            now = time.monotonic()
            if now - state["ts"] > 2.0:
                session = self._session()
                try:
                    job = session.get(ExportJob, job_id)
                    state["val"] = bool(job.cancel_requested) if job else False
                finally:
                    session.close()
                state["ts"] = now
            return state["val"]

        return check

    # ------------------------------------------------------------------ #
    # Generación asíncrona (corre en un worker de export_runner)          #
    # ------------------------------------------------------------------ #
    def run_job(self, job_id: int) -> None:
        """
        Genera el artefacto. **Nunca lanza**: todo fallo termina como estado del job.

        Es el único punto del módulo que sostiene una transacción de lectura contra la base
        de un tercero, así que la estructura es la de un pipeline con cierre garantizado:
        reclamo atómico → guard in-process → sesión de consistencia (context manager, cierre
        en ``finally``) → spool (se borra solo si algo falla) → estado final.
        """
        # 1) Reclamar ATÓMICAMENTE (pending → running) con UPDATE condicional + rowcount: si
        #    dos workers compiten por el mismo job, solo uno afecta una fila y el otro sale.
        session = self._session()
        try:
            claimed = (
                session.query(ExportJob)
                .filter(
                    ExportJob.id == job_id, ExportJob.status == EXPORT_STATUS_PENDING
                )
                .update(
                    {
                        ExportJob.status: EXPORT_STATUS_RUNNING,
                        ExportJob.started_at: _utcnow(),
                        ExportJob.phase: EXPORT_PHASE_PREAMBLE,
                        ExportJob.error: None,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if not claimed:
                return  # otro worker ya lo tomó (o el job no estaba pendiente)
        finally:
            session.close()

        # El kill switch pudo apagarse mientras el job esperaba en la cola. Se comprueba acá
        # además de en el endpoint: la vía de salida tiene que poder cerrarse SIN esperar a
        # que la cola se vacíe.
        if not EXPORT_ENABLED:
            self._set_status(
                job_id,
                EXPORT_STATUS_FAILED,
                finished=True,
                phase=EXPORT_PHASE_DONE,
                error="La exportación se deshabilitó en el gateway antes de generar el artefacto.",
            )
            return

        session = self._session()
        try:
            job = session.get(ExportJob, job_id)
            if job is None:
                return
            ctx = {
                "spec": json.loads(job.spec),
                "resolved": json.loads(job.resolved_selection or "{}"),
                "database": job.database_name,
                "engine": job.engine,
                "server_id": job.server_id,
                "managed_id": job.database_id,
                "fingerprint": job.source_fingerprint,
                "admin_id": job.created_by_admin_id,
            }
        finally:
            session.close()

        from app.services import export_runner

        target = self._server_target(ctx["server_id"])
        db_ref = f"{ctx['server_id']}:{ctx['database']}"
        with export_runner.database_guard(db_ref):
            try:
                self._generate(job_id, target, ctx)
            except _Canceled:
                self._set_status(
                    job_id,
                    EXPORT_STATUS_CANCELED,
                    finished=True,
                    phase=EXPORT_PHASE_DONE,
                    error="Cancelado por el operador; el artefacto parcial se descartó.",
                )
            except Exception as exc:
                logger.error(
                    "Exportación %s falló (request_id en el log de la excepción)",
                    job_id,
                    exc_info=True,
                )
                self._set_status(
                    job_id,
                    EXPORT_STATUS_FAILED,
                    finished=True,
                    phase=EXPORT_PHASE_DONE,
                    error=self._failure_reason(exc),
                )

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        """
        Motivo ACOTADO de un fallo, para persistir en el job.

        **Nunca ``str(exc)`` de un error del motor** (criterio R4, §9.5): el mensaje de un
        driver puede incrustar valores de filas, y el job es un registro que se conserva y se
        muestra por API. El detalle crudo va a ``logger.exception`` con el Request ID, que es
        donde tiene que estar para diagnosticar.

        Las ``AppHttpException`` sí se propagan tal cual: son mensajes que escribió el
        gateway, no el motor.
        """
        if isinstance(exc, AppHttpException):
            return exc.message[:500]
        if isinstance(exc, ExportDurationExceeded):
            return (
                f"La exportación superó el máximo de {EXPORT_MAX_DURATION_SECONDS} s y se "
                "abortó; la transacción contra el origen se cerró."
            )
        if isinstance(exc, export_storage.ArtifactTooLarge):
            return (
                "El artefacto superó el tamaño máximo permitido y se descartó. Reducí la "
                "selección o exportá por partes."
            )
        if isinstance(exc, export_storage.InsufficientDiskSpace):
            return (
                "No hay espacio suficiente en el gateway para el artefacto estimado; no se "
                "generó nada."
            )
        if isinstance(exc, UnicodeEncodeError):
            # Típico de elegir una codificación de archivo estrecha (latin-1 para Excel) y
            # encontrarse un valor que no entra. Es un error del gateway, no del motor, y el
            # operador necesita saber QUÉ opción cambiar.
            return (
                "Hay valores que no se pueden escribir con la codificación pedida en "
                "output.file_encoding; usá 'utf-8'."
            )
        if isinstance(exc, export_package.PackagingError):
            # Mensaje del gateway, no del motor: es seguro propagarlo tal cual y es lo único
            # que le dice al operador qué opción de salida tiene que cambiar.
            return f"No se pudo empaquetar el artefacto: {exc}"[:500]
        if isinstance(exc, SQLAlchemyError):
            return (
                "Error del motor de origen al leer la base; el detalle quedó en los logs "
                "del gateway (buscar por Request ID)."
            )
        return "Fallo inesperado al generar el artefacto; ver los logs del gateway."

    def _generate(self, job_id: int, target, ctx: dict) -> None:
        """
        El pipeline real: sesión de consistencia → snapshot → writer → spool → artefacto.

        Todo lo que toca el origen vive dentro de ``export_session``, cuyo ``finally`` cierra
        la transacción pase lo que pase. Una transacción huérfana contra la base de un
        tercero bloquea su ``VACUUM`` (PostgreSQL) o infla su historial de undo (familia
        MySQL) hasta que alguien la mata a mano: es un incidente de producción, no una fuga
        de recursos menor.
        """
        spec = espec.ExportSpec.from_dict(ctx["spec"])
        resolved = ctx["resolved"]
        objects = [
            espec.CatalogObject(object_type=o["object_type"], name=o["name"])
            for o in (resolved.get("objects") or [])
        ]
        data_tables = list(resolved.get("data") or [])
        database = ctx["database"]
        engine = ctx["engine"]

        # Espacio libre ANTES de abrir nada (§9.7): llenar el disco del gateway no degrada la
        # exportación, tumba el gateway entero.
        export_storage.ensure_capacity(int(resolved.get("estimated_bytes") or 0))

        adapter = get_adapter(target)
        engine_version = None
        try:
            engine_version = adapter.test_connection().server_version
        except Exception:  # noqa: BLE001 — es un metadato del encabezado, no una garantía
            logger.warning(
                "No se pudo leer la versión del motor para la exportación %s", job_id
            )

        cancel = self._cancel_checker(job_id)
        stats = ewriter.ExportStats()
        started = time.monotonic()
        artifact: dict | None = None
        drift = False

        with export_session(target, database, engine=engine) as sess:
            # El catálogo se lee DENTRO de la transacción del job (inyección de conexión del
            # §6.4): si se leyera con una conexión propia, la estructura y los datos serían
            # de dos instantes distintos y el artefacto podría llevar un INSERT para una
            # columna que ya no existe.
            snapshot = adapter.structural_snapshot(database, conn=sess.conn)
            if _snapshot_fingerprint(snapshot) != ctx["fingerprint"]:
                # Cuarta comprobación anti-TOCTOU (plan → preview → execute → worker): entre
                # la confirmación y el arranque real puede haber tiempo de cola.
                self._set_status(
                    job_id,
                    EXPORT_STATUS_FAILED,
                    finished=True,
                    phase=EXPORT_PHASE_DONE,
                    error=(
                        "El esquema del origen cambió antes de generar el artefacto; "
                        "volvé a previsualizar."
                    ),
                )
                return

            source = _ExportRowSource(sess, adapter, database)
            write_target = ewriter.ExportTarget(
                database=database,
                engine=engine,
                objects=objects,
                data_tables=data_tables,
                engine_version=engine_version,
                job_id=job_id,
                generated_at=_utcnow().isoformat(timespec="seconds"),
                consistent_structure=sess.supports_consistent_structure,
            )

            with export_storage.spool(encoding=spec.output.file_encoding) as handle:
                # El empaquetador se interpone entre el writer y el spool: aplica
                # organización, fragmentación y compresión (§10.3). Con un artefacto único
                # sin comprimir —el caso por defecto— es un pasamanos y los bytes son
                # exactamente los que escribía F4.
                with export_package.packager(
                    spec,
                    handle.write_bytes,
                    base_name=self._artifact_base_name(job_id, database, spec),
                ) as pack:
                    self._consume(job_id, spec, write_target, snapshot, adapter, source,
                                  pack, handle, stats, sess, cancel)
                    # El writer fija ``complete`` en su última línea, así que un corte por
                    # ``on_error='stop'`` (que sale del bucle con ``break``) nunca llega a
                    # marcarlo. Se recalcula acá con el mismo criterio: si hay un objeto en
                    # error, el artefacto NO está completo.
                    if any(i.status == EXPORT_ITEM_ERROR for i in stats.items):
                        stats.complete = False
                    # §14: un artefacto parcial NUNCA se entrega sin marca inequívoca. Dónde
                    # va esa marca lo decide el empaquetador, porque depende del formato: un
                    # comentario SQL pegado al final de un CSV o de un JSON los corrompe.
                    pack.finish(complete=stats.complete, job_id=job_id)

                # Re-verificación FINAL del fingerprint (§6.2), todavía dentro de la sesión.
                # En PostgreSQL el catálogo entra en el snapshot y por construcción no puede
                # haber cambiado; en MySQL/MariaDB el diccionario de datos NO participa del
                # MVCC, así que acá es donde aparece un ALTER concurrente. No invalida el
                # artefacto —los datos siguen siendo consistentes— pero el operador tiene que
                # enterarse.
                try:
                    drift = (
                        _snapshot_fingerprint(
                            adapter.structural_snapshot(database, conn=sess.conn)
                        )
                        != ctx["fingerprint"]
                    )
                except SQLAlchemyError:
                    logger.warning(
                        "No se pudo re-verificar el catálogo al cerrar la exportación %s",
                        job_id,
                    )

                artifact = export_storage.finalize(
                    job_id,
                    handle,
                    content_type=export_package.content_type(spec),
                    ttl_minutes=EXPORT_ARTIFACT_TTL_MINUTES,
                    part_count=pack.part_count,
                )

        # Fuera de la sesión: la transacción contra el origen ya está cerrada.
        self._record_items(job_id, stats.items)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._set_progress(
            job_id,
            self._progress_payload(
                stats,
                phase=EXPORT_PHASE_DONE,
                engine_version=engine_version,
                degradations=list(sess.degradations),
                artifact=artifact,
                elapsed_ms=elapsed_ms,
            ),
        )
        failures = [i for i in stats.items if i.status == EXPORT_ITEM_ERROR]
        self._set_status(
            job_id,
            EXPORT_STATUS_SUCCEEDED if stats.complete else EXPORT_STATUS_FAILED,
            finished=True,
            phase=EXPORT_PHASE_DONE,
            drift=drift,
            error=(
                None
                if stats.complete
                else (
                    f"{len(failures)} objeto(s) no se pudieron exportar; el artefacto está "
                    "marcado como incompleto (ver el reporte de incidencias)."
                )
            ),
        )
        audit.record(
            "database_export.execute",
            status="success" if stats.complete else "error",
            admin={"id": ctx["admin_id"]},
            target_type=(
                "managed_database" if ctx["managed_id"] is not None else "server_database"
            ),
            target_id=ctx["managed_id"],
            server_id=ctx["server_id"],
            touched_engine=True,
            detail=(
                f"exportación {job_id} de {ctx['server_id']}/{database}: "
                f"{stats.objects_exported} objeto(s), {stats.rows_exported} fila(s), "
                f"{(artifact or {}).get('byte_size', 0)} bytes, "
                f"completa={stats.complete}, drift={drift}"
            ),
        )

    def _consume(
        self, job_id, spec, write_target, snapshot, adapter, source, pack, handle, stats,
        sess, cancel,
    ) -> None:
        """
        Consume el generador del writer hacia el empaquetador, con cancelación y progreso.

        Se consume el generador trozo a trozo (y no un ``write_sql`` opaco) justamente para
        poder intercalar los tres controles que de otro modo no cabrían: la cancelación
        cooperativa, el plazo duro de la sesión y el progreso THROTTLEADO (~3 s). Sin el
        throttle, una tabla de millones de filas dispara un UPDATE contra la BD del gateway
        por cada trozo.

        El formato lo elige ``iter_artifact`` a partir del spec: acá no hay ni un ``if`` por
        formato, igual que el writer no tiene ninguno por motor.
        """
        last_persist = 0.0
        emitted = 0
        stop_on_error = spec.on_error == espec.OnError.stop

        for chunk in ewriter.iter_artifact(
            spec, write_target, snapshot, adapter, source, stats
        ):
            pack.write(chunk)
            emitted += 1
            if emitted % _CANCEL_CHECK_EVERY == 0:
                if cancel():
                    raise _Canceled()
                # Plazo duro: lo comprueba la sesión, que es la dueña del reloj y de la
                # transacción. Cooperativo a propósito (ver ``ExportSession.check_deadline``).
                sess.check_deadline()
                now = time.monotonic()
                if now - last_persist >= _PROGRESS_PERSIST_SECONDS:
                    last_persist = now
                    self._set_progress(
                        job_id,
                        self._progress_payload(
                            stats,
                            phase=self._phase_of_stats(stats),
                            bytes_written=handle.size,
                        ),
                    )
            if stop_on_error and any(
                i.status == EXPORT_ITEM_ERROR for i in stats.items
            ):
                # ``on_error='stop'``: se deja de generar en el primer objeto fallido. El
                # artefacto queda TRUNCADO —sin epílogo— y por eso sale marcado como
                # incompleto; entregarlo sin marca sería lo único inaceptable.
                stats.warn(
                    "La generación se detuvo en el primer objeto fallido (on_error='stop'): "
                    "el artefacto está truncado y no lleva el epílogo de sesión."
                )
                break

    @staticmethod
    def _phase_of_stats(stats) -> str:
        """Fase legible a partir de lo que el writer ya emitió (solo para la interfaz)."""
        if stats.tables_with_data:
            return EXPORT_PHASE_DATA
        if stats.objects_exported:
            return EXPORT_PHASE_STRUCTURE
        return EXPORT_PHASE_PREAMBLE

    @staticmethod
    def _progress_payload(
        stats,
        *,
        phase: str,
        bytes_written: int | None = None,
        engine_version: str | None = None,
        degradations: list[str] | None = None,
        artifact: dict | None = None,
        elapsed_ms: int | None = None,
    ) -> dict:
        """
        El JSON de ``export_jobs.progress``.

        Lleva también dos metadatos del artefacto (versión del generador y del motor) porque
        el manifiesto (§10.4) los exige y el modelo de F2 no tiene columnas para ellos. Es un
        dato de presentación, no un invariante: inventarse una migración para dos cadenas
        habría costado más que documentarlo acá.
        """
        payload: dict = {
            "phase": phase,
            "objects": stats.objects_exported,
            "rows": stats.rows_exported,
            "statements": stats.statements,
            "tables_with_data": stats.tables_with_data,
            "bytes": bytes_written if bytes_written is not None else stats.bytes_written,
            "warnings": list(stats.warnings),
            "generator_version": ewriter.GENERATOR_VERSION,
        }
        if engine_version:
            payload["engine_version"] = engine_version
        if degradations:
            payload["degradations"] = degradations
        if artifact:
            payload["artifact"] = {
                "byte_size": artifact.get("byte_size"),
                "sha256": artifact.get("sha256"),
                "part_count": artifact.get("part_count"),
            }
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        return payload

    # ------------------------------------------------------------------ #
    # 10) Manifiesto (§10.4)                                              #
    # ------------------------------------------------------------------ #
    def manifest(self, job_id: int) -> dict:
        """
        Inventario verificable del artefacto: qué salió, cuánto pesa y con qué checksum.

        Permite comprobar integridad y auditar **sin abrir el archivo**, que es justo lo que
        se quiere de una exportación de datos: mirar el contenido para saber qué se llevó
        sería una segunda divulgación.
        """
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            spec = json.loads(job.spec) if job.spec else {}
            progress = json.loads(job.progress) if job.progress else {}
            items = (
                session.query(ExportJobItem)
                .filter(ExportJobItem.job_id == job_id)
                .order_by(ExportJobItem.seq.asc())
                .all()
            )
            summary = {
                "job_id": job.id,
                "engine": job.engine,
                "database": job.database_name,
                "status": job.status,
                "structure_drift_detected": job.structure_drift_detected,
                "created_at": job.created_at,
            }
            rows = [
                {
                    "object_type": i.object_type,
                    "name": i.object_name,
                    "status": i.status,
                    "rows_exported": i.rows_exported,
                    "bytes_written": i.bytes_written,
                    "deterministic": i.deterministic,
                    "reason": i.reason,
                }
                for i in items
            ]
        finally:
            session.close()

        artifact = export_storage.describe(job_id) or {}
        return {
            "job_id": summary["job_id"],
            "engine": summary["engine"],
            "engine_version": progress.get("engine_version"),
            "database": summary["database"],
            "format": spec.get("format") or espec.Format.sql.value,
            # ``complete`` sale del ESTADO del job y no de un recuento propio: el estado ya
            # es ``succeeded`` únicamente cuando todos los objetos salieron ``ok``, y tener
            # dos fuentes para lo mismo es cómo terminan discrepando.
            "complete": summary["status"] == EXPORT_STATUS_SUCCEEDED,
            "structure_drift_detected": summary["structure_drift_detected"],
            "generator_version": progress.get("generator_version"),
            "spec": spec,
            "objects": rows,
            "total_rows": sum(i["rows_exported"] or 0 for i in rows),
            "byte_size": artifact.get("byte_size"),
            "sha256": artifact.get("sha256"),
            "part_count": artifact.get("part_count"),
            "created_at": summary["created_at"],
            "expires_at": artifact.get("expires_at"),
        }

    # ------------------------------------------------------------------ #
    # 11-12) Entrega (§10.2) — el punto de DIVULGACIÓN                    #
    # ------------------------------------------------------------------ #
    def prepare_download(self, job_id: int, *, admin: dict | None, inline: bool) -> dict:
        """
        Valida, **audita fail-closed** y devuelve por dónde entregar el artefacto.

        Este es el punto crítico del §9.4 y replica el patrón de
        ``server_user_controller.reveal_password``: la intención se registra ANTES de abrir
        el archivo, y si la auditoría no se persiste se aborta sin que salga un solo byte.
        Una exportación de datos **es una divulgación**, no una lectura más — y como no hay
        enmascarado (§9.6), el rastro es el único control que queda en pie.

        Devuelve metadatos (ruta, nombre, tamaño, checksum, si está completo); la ruta nunca
        viaja al cliente: la consume la capa de rutas para construir la respuesta.
        """
        self._guard_enabled()
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._guard_owner(job, admin)
            status = job.status
            database = job.database_name
            server_id = job.server_id
            managed_id = job.database_id
            spec = espec.ExportSpec.from_dict(json.loads(job.spec))
        finally:
            session.close()

        if status in (EXPORT_STATUS_PENDING, EXPORT_STATUS_RUNNING):
            raise AppHttpException(
                message=f"La exportación todavía está en estado '{status}'.",
                status_code=409,
                public_context={"code": "export.not_ready", "status": status},
            )

        artifact = export_storage.describe(job_id)
        if artifact is None:
            raise AppHttpException(
                message="Esta exportación no produjo ningún artefacto.",
                status_code=409,
                public_context={"code": "export.no_artifact", "status": status},
            )
        if artifact["state"] == EXPORT_ARTIFACT_CONSUMED:
            raise AppHttpException(
                message=(
                    "El artefacto ya se descargó y se borró (descarga de un solo uso). "
                    "Volvé a exportar si lo necesitás de nuevo."
                ),
                status_code=410,
                public_context={"code": espec.CODE_ARTIFACT_CONSUMED},
            )
        if artifact["state"] != EXPORT_ARTIFACT_AVAILABLE or artifact["expired"]:
            raise AppHttpException(
                message="El artefacto venció y se borró por retención.",
                status_code=410,
                public_context={"code": espec.CODE_ARTIFACT_EXPIRED},
            )
        if inline and artifact["byte_size"] > EXPORT_INLINE_MAX_BYTES:
            # NUNCA se trunca en silencio: un script cortado que alguien pega y ejecuta es
            # peor que un fallo. El 409 es accionable — trae el tamaño y la salida.
            raise AppHttpException(
                message=(
                    f"El artefacto pesa {artifact['byte_size']} bytes y la entrega en línea "
                    f"admite hasta {EXPORT_INLINE_MAX_BYTES}. Descargalo como archivo."
                ),
                status_code=409,
                public_context={
                    "code": espec.CODE_INLINE_TOO_LARGE,
                    "field": "output.delivery",
                    "byte_size": artifact["byte_size"],
                    "inline_max_bytes": EXPORT_INLINE_MAX_BYTES,
                },
            )

        audit.record_intent(
            "database_export.download",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            # No se toca el motor: el artefacto ya está en disco. El rastro se exige igual
            # (mismo criterio que revelar una contraseña) porque lo que ocurre acá es la
            # DIVULGACIÓN de los datos, no su lectura del origen.
            touched_engine=False,
            detail=(
                f"INTENT descargar el artefacto de la exportación {job_id} "
                f"({server_id}/{database}, {artifact['byte_size']} bytes, "
                f"{'en línea' if inline else 'archivo'})"
            ),
        )

        path = export_storage.path_for(artifact["storage_name"])
        if not path.exists():
            raise AppHttpException(
                message=(
                    "El archivo del artefacto ya no está en disco (se purgó o el proceso "
                    "se reinició a mitad de la generación)."
                ),
                status_code=410,
                public_context={"code": espec.CODE_ARTIFACT_EXPIRED},
            )

        complete = status == EXPORT_STATUS_SUCCEEDED
        return {
            "path": path,
            "filename": self._artifact_filename(job_id, database, spec),
            "media_type": "text/plain" if inline else artifact["content_type"],
            "byte_size": artifact["byte_size"],
            "sha256": artifact["sha256"],
            "encoding": spec.output.file_encoding,
            "complete": complete,
            "single_use": EXPORT_SINGLE_USE_DOWNLOAD,
        }

    def finish_delivery(
        self, job_id: int, *, admin: dict | None = None, consume: bool = True
    ) -> None:
        """
        Cierra la entrega: cuenta la descarga y, con un solo uso, borra el archivo.

        Se invoca DESPUÉS de que la respuesta terminó de enviarse (tarea de fondo). Borrar
        antes dejaría al cliente con una descarga cortada a la mitad — el archivo se está
        leyendo mientras se envía.

        ``consume=False`` es para una descarga **genuinamente parcial** (``Range`` que NO
        cubre el archivo entero): ahí borrarlo rompería la reanudación que el ``Range``
        habilita. El CONTADOR, en cambio, se incrementa SIEMPRE: es el rastro de cuántas
        veces salió el artefacto, y no contabilizar las entregas parciales convertía el
        registro en una subestimación silenciosa — justo con el método que además evitaba
        el borrado.
        """
        export_storage.mark_downloaded(job_id)
        deleted = consume and EXPORT_SINGLE_USE_DOWNLOAD
        if deleted:
            export_storage.consume(job_id)
        audit.record(
            "database_export.download",
            admin=admin,
            touched_engine=False,
            detail=(
                f"artefacto de la exportación {job_id} entregado"
                + ("" if consume else " (parcial, por rango)")
                + (" y borrado (un solo uso)" if deleted else "")
            ),
        )

    def read_inline(self, job_id: int, *, admin: dict | None) -> dict:
        """
        Entrega EN LÍNEA: el artefacto como texto plano, para copiar al portapapeles.

        Materializarlo en memoria es aceptable **solo** acá porque
        ``EXPORT_INLINE_MAX_BYTES`` ya lo acotó en ``prepare_download``; el modo archivo
        nunca lo hace.
        """
        info = self.prepare_download(job_id, admin=admin, inline=True)
        text = info["path"].read_text(encoding=info["encoding"], errors="replace")
        self.finish_delivery(job_id, admin=admin)
        return {**info, "text": text}

    @staticmethod
    def _artifact_base_name(job_id: int, database: str, spec: espec.ExportSpec) -> str:
        """
        Nombre del artefacto SIN extensión, construido y saneado por el SERVIDOR.

        El cliente nunca envía ni recibe una ruta: solo elige una plantilla con tokens de una
        whitelist cerrada, y ``sanitize_filename_template`` neutraliza el resto.

        ``{object}`` se sustituye por vacío también acá: la plantilla nombra el ARTEFACTO, no
        cada archivo de adentro. Los nombres de las entradas de un contenedor los construye
        el writer, porque llevan el ordinal que documenta el orden de ejecución y dos
        entradas homónimas se pisarían al descomprimir.
        """
        now = _utcnow()
        return espec.sanitize_filename_template(
            spec.output.filename_template,
            {
                "database": database,
                "date": now.strftime("%Y%m%d"),
                "time": now.strftime("%H%M%S"),
                "job_id": str(job_id),
                "object": "",
            },
        )

    @classmethod
    def _artifact_filename(cls, job_id: int, database: str, spec: espec.ExportSpec) -> str:
        """
        El nombre de la descarga, con la extensión de lo que REALMENTE se entrega.

        La extensión sale de ``export_package``, que la deriva de la compresión efectiva: un
        artefacto multiarchivo se eleva a zip aunque se haya pedido sin comprimir, y ofrecer
        un ``.sql`` que en realidad es un zip haría que el navegador lo abriera mal.
        """
        base = cls._artifact_base_name(job_id, database, spec)
        return f"{base}{export_package.artifact_extension(spec)}"

    @staticmethod
    def _guard_owner(job: ExportJob, admin: dict | None) -> None:
        """
        Quien descarga tiene que ser quien exportó (§9.3).

        Hoy hay un solo admin y la comprobación es trivialmente cierta. Se escribe igual —y
        se comprueba en el punto de divulgación, no al planear— para que el día que exista
        multiusuario esto no sea un agujero que nadie recuerde abrir. Si el job no guardó
        autor (planes creados antes de tener el campo), no se bloquea: negar el acceso a un
        artefacto propio por un dato ausente sería peor que el riesgo que evita.
        """
        owner = job.created_by_admin_id
        current = (admin or {}).get("id")
        if owner is None or current is None or owner == current:
            return
        raise AppHttpException(
            message="Esta exportación la creó otro administrador.",
            status_code=403,
            public_context={"code": "export.not_owner"},
            context={"export_job_id": job.id},
        )


# --------------------------------------------------------------------------- #
# Helpers de módulo                                                            #
# --------------------------------------------------------------------------- #


class _Canceled(Exception):
    """
    Señal interna de cancelación cooperativa.

    Es una excepción y no un ``return`` a propósito: propagarla desliga el corte del punto
    exacto en que se detectó y hace que los dos ``context manager`` que hay abiertos
    (la sesión de consistencia y el spool) ejecuten su limpieza — cerrar la transacción
    contra el origen y borrar el artefacto parcial— sin que el código de corte tenga que
    acordarse de nada.
    """


class _ExportRowSource:
    """
    El ``RowSource`` del writer sobre la sesión de consistencia del job.

    Existe para que el writer siga sin saber nada de conexiones (su costura testeable) y
    para implementar el hook ``counter_value``, que F3 dejó declarado pero sin fuente: el
    valor del contador de autoincremento es ESTADO, no estructura, así que el
    ``SchemaSnapshot`` deliberadamente no lo captura y hay que leerlo del motor — dentro de
    la MISMA transacción, o describiría otro instante que las filas.
    """

    def __init__(self, session, adapter, database: str):
        self._session = session
        self._adapter = adapter
        self._database = database

    def iter_rows(self, select_sql: str, *, batch_rows: int = EXPORT_BATCH_ROWS):
        return self._session.iter_rows(select_sql, batch_rows=batch_rows)

    def counter_value(self, table: str, column: str) -> int | None:
        """
        Contador actual de una tabla, o ``None`` si el motor no lo expone o falla la lectura.

        La consulta la da el adapter (``export_counter_value_sql``), que es también quien
        rendea el ``reset``: las dos mitades tienen que compartir semántica —el próximo id en
        MySQL, el último usado en PostgreSQL— y sepradas divergirían.

        Best-effort DELIBERADO: si la lectura falla, se devuelve ``None`` y el artefacto sale
        sin ajuste de contador. Nunca se inventa un valor —un contador equivocado deja la
        tabla generando ids que ya existen— y nunca se aborta la exportación entera por un
        metadato accesorio. ``ExportDurationExceeded`` sí se propaga: es el plazo duro del
        job, no un fallo del motor.
        """
        query = self._adapter.export_counter_value_sql(self._database, table, column)
        if query is None:
            return None
        sql, params = query
        try:
            value = self._session.scalar(sql, params)
        except SQLAlchemyError:
            logger.warning(
                "No se pudo leer el contador de autoincremento de %s.%s; el artefacto sale "
                "sin ajuste de contador para esa tabla",
                self._database,
                table,
            )
            return None
        return int(value) if value is not None else None


def _scope_note(engine: str) -> str | None:
    if engine == "postgresql":
        return "PostgreSQL: solo el schema 'public'."
    return None


def _data_only(spec: espec.ExportSpec) -> bool:
    """La excepción del §5.3: sin DDL de ningún nivel, la exportación es "solo datos"."""
    return (
        spec.structure.scope_ddl == espec.ScopeDdl.NONE
        and spec.structure.entity_ddl == espec.EntityDdl.NONE
    )


def _prune_unsatisfied(
    snapshot: SchemaSnapshot, selected: set[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """
    Poda TRANSITIVA: quita todo objeto cuya dependencia autoritativa quedó fuera.

    Devuelve ``(conjunto podado, objetos eliminados)``. Es la contracara del 422 de la
    selección explícita: en una selección automática el usuario describió un criterio, no
    una lista, así que completar el cierre agregando objetos que su criterio excluye sería
    desobedecerlo — lo correcto es sacar al dependiente y decirlo.

    Se itera hasta punto fijo porque la poda es contagiosa: sacar una tabla puede dejar
    huérfano al trigger que vive sobre ella, y a la tabla que la referencia por FK.
    """
    edges, _advisory = cdeps.build_graph(snapshot)
    kept = set(selected)
    pruned: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for edge in edges:
            src = (edge.from_type, edge.from_name)
            dst = (edge.to_type, edge.to_name)
            if src in kept and dst not in kept:
                kept.discard(src)
                pruned.add(src)
                changed = True
    return kept, pruned
