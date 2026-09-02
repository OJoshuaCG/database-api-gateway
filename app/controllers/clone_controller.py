"""
Controller de clonación de bases de datos (feature "clone database").

Clona la estructura (y opcionalmente los datos) de una BD ORIGEN hacia una BD DESTINO
en cualquier servidor dado de alta — el mismo u otro, mismo motor o distinto. Ni el
origen ni el destino necesitan estar adoptados por el gateway; el destino puede no
existir todavía.

Flujo (mismo patrón seguro que schema-comparisons): crear PLAN (snapshotea el origen,
persiste cabecera + fingerprint) → inspeccionar objetos/dependencias/portabilidad →
resolver selección (cierre de dependencias) → PREVIEW (resuelve el plan final + token,
sin ejecutar) → EXECUTE (valida token/nombre/fingerprint, encola el job asíncrono).

Este archivo cubre el lado PLAN/PREVIEW (solo lectura del motor). La ejecución asíncrona
vive en ``app/services/clone_runner.py`` y se dispara desde ``execute_clone``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, text
from sqlalchemy.exc import SQLAlchemyError

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.controllers.schema_comparison_controller import _synthetic_lock_key
from app.core.database import Database
from app.core.environments import (
    CLONE_DATA_BATCH_ROWS,
    CLONE_TTL_HOURS,
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
)
from app.core.logger import get_logger
from app.core.remote_engine import ServerTarget, database_connection
from app.exceptions import AppHttpException
from app.models.clone_job import (
    CLONE_CLEAN_DROP_DATABASE,
    CLONE_CLEAN_NONE,
    CLONE_CLEAN_OBJECTS,
    CLONE_COPY_DATA_ONLY,
    CLONE_COPY_STRUCTURE_AND_DATA,
    CLONE_COPY_STRUCTURE_ONLY,
    CLONE_ITEM_ADOPT,
    CLONE_ITEM_APPLIED,
    CLONE_ITEM_CLEAN,
    CLONE_ITEM_DATA,
    CLONE_ITEM_FAILED,
    CLONE_ITEM_SKIPPED,
    CLONE_ITEM_STRUCTURE,
    CLONE_STATUS_CANCELED,
    CLONE_STATUS_FAILED,
    CLONE_STATUS_INTERRUPTED,
    CLONE_STATUS_PENDING,
    CLONE_STATUS_RUNNING,
    CLONE_STATUS_SUCCEEDED,
    CLONE_TARGET_EXISTING,
    CLONE_TARGET_NEW,
    CloneJob,
    CloneJobItem,
)
from app.models.enums import EngineType, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.server_user import ServerUser
from app.services import audit, charset_catalog
from app.services.db_admin import clone_dependencies as cdeps
from app.services.db_admin import clone_spec as cspec
from app.services.db_admin import export_spec as espec
from app.services.db_admin import query_policy
from app.services.db_admin.data_copy import TableCopySpec, copy_tables
from app.services.db_admin.dtos import SchemaSnapshot
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import (
    ensure_not_reserved_database,
    quote_identifier,
    validate_identifier,
)
from app.services.db_admin.migrations import MigrationRunner
from app.services.db_admin.schema_diff import diff_snapshots
from app.services.db_admin.sql_dialect import (
    BODY_OBJECT_TYPES as _BODY_TYPES,
)
from app.services.db_admin.sql_dialect import (
    requalify_body_schema,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class _StructStmt:
    kind: str  # 'clean' | 'structure'
    object_type: str
    object_name: str
    sql: str


@dataclass(frozen=True)
class _DataSpec:
    table: str
    columns: list[str]
    primary_key: list[str]
    upsert: bool
    row_estimate: int | None
    # ``row_estimate=None`` es ambiguo por sí solo (¿no se pidió, o el catálogo no sabe?).
    # Este flag lo desambigua para la UI. NO entra al ``confirm_token``: un ANALYZE de fondo
    # entre el preview y el execute invalidaría el token sin que el plan haya cambiado.
    row_estimate_known: bool = True
    has_primary_key: bool | None = None
    # ¿La tabla tiene alguna clave única? Decide si la copia bulk pasa por una tabla de
    # staging: sin ella, una tabla SIN PK pero CON UNIQUE se cargaba directo a la final y
    # el IGNORE implícito de LOAD DATA LOCAL descartaba el conflicto en silencio.
    has_unique_key: bool = False


@dataclass(frozen=True)
class _ExecutionPlan:
    clean_statements: list[_StructStmt]
    structure_statements: list[_StructStmt]
    data_specs: list[_DataSpec]
    skipped: list[dict]
    will_adopt: bool
    table_order: list[str]
    warnings: list[str]
    # Incompatibilidades del DESTINO que impiden ejecutar. El plan las TRANSPORTA en vez de
    # lanzar: si el guard lanzara desde acá, el ``preview`` no podría renderizar nada y el
    # operador recibiría "incompatible" sin ver el plan ni el resto de los avisos. Cada
    # llamador decide (preview → 200 sin token; execute y worker → rechazo).
    blocking_issues: list[dict] = dataclass_field(default_factory=list)
    notices: list[dict] = dataclass_field(default_factory=list)

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
# Tipos con cuerpo procedural: no portables cross-engine (atados al motor de origen).
_PROCEDURAL_TYPES = frozenset({"routine", "trigger", "event"})
# Tipos específicos de un motor sin equivalente directo cross-family.
_ENGINE_SPECIFIC_TYPES = frozenset(
    {"sequence", "enum_type", "extension", "materialized_view"}
)
# ``_BODY_TYPES`` se importa como alias de ``BODY_OBJECT_TYPES`` (``sql_dialect``, fuente
# única de verdad compartida con schema-comparison): tipos cuyo DDL lleva un CUERPO que puede
# referenciar OTROS objetos por nombre (vistas/rutinas/triggers/eventos). Requieren: (1)
# re-calificar el esquema origen→destino y (2) ejecución con reintento diferido, porque
# pueden depender entre sí en cualquier orden.
# Objetos con cuerpo que tienen EFECTOS SECUNDARIOS ante un INSERT: un trigger de origen
# puede poblar OTRA tabla, y un evento puede mutar datos. Se crean DESPUÉS de la fase de
# datos (ver _run_phases) para que no se disparen durante la copia y dupliquen/alteren filas
# (MySQL/MariaDB NO desactivan triggers con FOREIGN_KEY_CHECKS=0; PostgreSQL sí con
# session_replication_role='replica', pero diferirlos es la defensa portable para ambos).
_POST_DATA_BODY_TYPES = frozenset({"trigger", "event"})
# Intervalo mínimo entre persistencias del progreso de datos a la BD del gateway (throttle).
_CLONE_PROGRESS_PERSIST_SECONDS = 3.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _same_family(a: str, b: str) -> bool:
    return a == b or (a in _MYSQL_FAMILY and b in _MYSQL_FAMILY)


def _snapshot_fingerprint(snapshot) -> str:
    """Hash estable del snapshot NORMALIZADO (anti-TOCTOU). Excluye lo cosmético."""
    payload = snapshot.model_dump(mode="json")
    payload.pop("captured_at", None)
    for ext in payload.get("extensions") or []:
        ext.pop("version", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _ResolvedSide:
    server_id: int
    database_name: str
    engine: str
    target: ServerTarget
    managed_id: int | None
    model_id: int | None
    model_version: str | None
    quarantined: bool
    exists_live: bool


class CloneController:
    def __init__(self, *, cache_source_snapshot: bool = False):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
        # Caché del snapshot del ORIGEN, apagada por default. Ver ``_source_snapshot``.
        self._snap_cache: dict[tuple[int, str], SchemaSnapshot] | None = (
            {} if cache_source_snapshot else None
        )

    def _session(self):
        return self.db.get_declarative_base_session()

    def _source_snapshot(self, src_target: ServerTarget, database: str) -> SchemaSnapshot:
        """
        Snapshot del origen, opcionalmente cacheado por la vida de este controller.

        Armar un plan snapshotea el origen hasta TRES veces seguidas contra el mismo servidor:
        una en ``create_plan`` (para el fingerprint anti-TOCTOU), otra en ``_apply_spec`` si la
        spec trae ``structure`` o ``data`` (para resolver la selección declarativa contra el
        catálogo) y otra en ``_snapshots_for`` (para rendear el plan). Las tres calculan lo
        mismo, con segundos de diferencia, y cada una es una conexión nueva más ~3 consultas a
        ``information_schema`` — el costo dominante de un clon de una base chica.

        **La caché está APAGADA por default y solo la enciende el LOTE.** En el asistente de a
        una, ``create_plan`` y ``preview`` los dispara el operador desde el navegador y pueden
        estar separados por minutos de navegación: ahí re-snapshotear es lo correcto, porque el
        origen pudo cambiar y el operador tiene que ver el plan de lo que hay AHORA. En el lote
        las tres llamadas ocurren dentro de la misma función y en el mismo segundo.

        Lo que NUNCA se cachea es el snapshot que el worker toma bajo el advisory lock
        (``_pipeline``): ése es la garantía anti-TOCTOU real —compara contra el fingerprint y
        aborta si el origen cambió— y llama al adapter directo, sin pasar por acá.
        """
        if self._snap_cache is None:
            return get_adapter(src_target).structural_snapshot(database)
        clave = (src_target.server_id, database)
        if clave not in self._snap_cache:
            self._snap_cache[clave] = get_adapter(src_target).structural_snapshot(database)
        return self._snap_cache[clave]

    def forget_snapshots(self) -> None:
        """
        Vacía la caché de snapshots, si está encendida.

        El lote la llama antes de ejecutar. Hoy ``_pipeline`` no pasa por ``_source_snapshot``,
        así que sería redundante — pero la invariante «el worker nunca ve un snapshot cacheado»
        es de CORRECTITUD, no de rendimiento, y no puede depender de que nadie enrute una
        llamada nueva por acá sin darse cuenta. Si eso pasara, el clon validaría el fingerprint
        contra una foto vieja y el guard anti-TOCTOU quedaría apagado en silencio.
        """
        if self._snap_cache is not None:
            self._snap_cache.clear()

    # ------------------------------------------------------------------ #
    # Carga / resolución                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _db_or_404(session, db_id: int) -> ManagedDatabase:
        md = session.get(ManagedDatabase, db_id)
        if md is None:
            raise AppHttpException(
                message="Base de datos gestionada no encontrada.",
                status_code=404,
                context={"managed_database_id": db_id},
            )
        return md

    def _job_or_404(self, session, job_id: int) -> CloneJob:
        job = session.get(CloneJob, job_id)
        if job is None:
            raise AppHttpException(
                message="Job de clonación no encontrado.",
                status_code=404,
                context={"clone_job_id": job_id},
            )
        return job

    def _resolve_source(
        self, session, *, database_id, server_id, database_name
    ) -> _ResolvedSide:
        """Resuelve el ORIGEN (id de inventario o server+nombre crudo)."""
        if database_id is not None:
            md = self._db_or_404(session, database_id)
            server = get_server_or_404(session, md.server_id)
            return _ResolvedSide(
                server_id=md.server_id, database_name=md.name,
                engine=engine_value(server), target=build_target(server),
                managed_id=md.id, model_id=md.model_id, model_version=md.model_version,
                quarantined=md.status == ProvisionStatus.error, exists_live=True,
            )
        server = get_server_or_404(session, server_id)
        md = (
            session.query(ManagedDatabase)
            .filter(ManagedDatabase.server_id == server_id, ManagedDatabase.name == database_name)
            .one_or_none()
        )
        if md is not None:
            return _ResolvedSide(
                server_id=server_id, database_name=md.name,
                engine=engine_value(server), target=build_target(server),
                managed_id=md.id, model_id=md.model_id, model_version=md.model_version,
                quarantined=md.status == ProvisionStatus.error, exists_live=True,
            )
        return _ResolvedSide(
            server_id=server_id, database_name=database_name,
            engine=engine_value(server), target=build_target(server),
            managed_id=None, model_id=None, model_version=None,
            quarantined=False, exists_live=True,  # se valida en vivo abajo
        )

    def _resolve_target(self, session, *, server_id, database_name) -> _ResolvedSide:
        """Resuelve el DESTINO (siempre server+nombre; puede no existir todavía)."""
        server = get_server_or_404(session, server_id)
        md = (
            session.query(ManagedDatabase)
            .filter(ManagedDatabase.server_id == server_id, ManagedDatabase.name == database_name)
            .one_or_none()
        )
        return _ResolvedSide(
            server_id=server_id, database_name=database_name,
            engine=engine_value(server), target=build_target(server),
            managed_id=md.id if md else None,
            model_id=md.model_id if md else None,
            model_version=md.model_version if md else None,
            quarantined=(md is not None and md.status == ProvisionStatus.error),
            exists_live=False,  # se determina en vivo abajo
        )

    # ------------------------------------------------------------------ #
    # Portabilidad                                                        #
    # ------------------------------------------------------------------ #
    def _portability(self, object_type: str, src_engine: str, tgt_engine: str) -> tuple[bool, str | None]:
        """
        ¿Se puede clonar un objeto de este tipo del motor origen al destino?

        - Mismo motor / misma familia (MySQL↔MariaDB): todo portable.
        - Cross-family: solo estructura de tablas/vistas es best-effort traducible en la
          dirección MySQL→PostgreSQL (única que soporta ``SqlTranslator``/``render_diff``
          nativo). Cuerpos procedurales y objetos específicos del motor no son portables.
        """
        if _same_family(src_engine, tgt_engine):
            return True, None
        # Cross-family. La traducción nativa (render_diff con el adapter destino) cubre
        # bien tablas; el resto es limitado.
        if object_type == "table":
            return True, None
        if object_type == "view":
            return True, "vista: traducción best-effort del cuerpo (revisar antes de usar)"
        if object_type in _PROCEDURAL_TYPES:
            return False, "cuerpo procedural atado al motor de origen: no portable entre motores"
        if object_type in _ENGINE_SPECIFIC_TYPES:
            return False, "objeto específico del motor de origen sin equivalente directo en el destino"
        return False, "no portable entre motores"

    # ------------------------------------------------------------------ #
    # Inventario + dependencias                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _iter_objects(snap: SchemaSnapshot):
        """Enumera (object_type, name) de todos los objetos de primer nivel del snapshot."""
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

    def _build_inventory(
        self, snap: SchemaSnapshot, tgt_engine: str, *, include_data: bool, stats: dict | None = None
    ) -> dict:
        """
        Inventario de objetos + portabilidad + grafo de dependencias.

        ``stats`` viene del catálogo del motor origen (``list_table_stats``). Este campo
        estaba en el contrato desde el principio pero el diccionario que lo alimentaba nacía
        vacío y nunca se poblaba, así que la UI no podía mostrar el tamaño de lo que se va a
        copiar. ``row_estimate_known=False`` distingue "0 filas" de "el catálogo no lo sabe".
        """
        row_est: dict = stats or {}
        objects = []
        for otype, name in self._iter_objects(snap):
            portable, reason = self._portability(otype, snap.source_engine, tgt_engine)
            st = row_est.get(name) if otype == "table" else None
            objects.append({
                "object_type": otype, "name": name,
                "portable": portable, "portability_reason": reason,
                "row_estimate": (
                    st.estimated_rows if (st is not None and include_data) else None
                ),
                "row_estimate_known": st.estimated_rows_known if st is not None else True,
                "has_primary_key": st.has_primary_key if st is not None else None,
            })
        auth, advisory = cdeps.build_graph(snap)
        cross = not _same_family(snap.source_engine, tgt_engine)
        scope_note = None
        if snap.source_engine == "postgresql" or tgt_engine == "postgresql":
            scope_note = "PostgreSQL: solo el schema 'public'."
        return {
            "objects": objects,
            "authoritative_edges": [e.model_dump() for e in auth],
            "advisory_edges": [e.model_dump() for e in advisory],
            "cross_engine": cross,
            "scope_note": scope_note,
        }

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    def _serialize_summary(self, job: CloneJob) -> dict:
        return {
            "id": job.id,
            "source_server_id": job.source_server_id,
            "source_database_name": job.source_database_name,
            "source_database_id": job.source_database_id,
            "source_engine": job.source_engine,
            "target_server_id": job.target_server_id,
            "target_database_name": job.target_database_name,
            "target_database_id": job.target_database_id,
            "target_engine": job.target_engine,
            "target_mode": job.target_mode,
            "include_data": job.include_data,
            # `copy_intent` además de `include_data`: el booleano legacy no distingue
            # `data_only` de `structure_only`, así que un cliente que lo derivara de ahí
            # mostraría mal el modo de solo-datos. El modelo lo persiste desde el trabajo
            # de solo datos; solo faltaba exponerlo.
            "copy_intent": job.copy_intent or CLONE_COPY_STRUCTURE_ONLY,
            "clean_mode": job.clean_mode,
            "adopt_target": job.adopt_target,
            "cross_engine": not _same_family(job.source_engine, job.target_engine),
            "status": job.status,
            "phase": job.phase,
            "progress": json.loads(job.progress) if job.progress else None,
            "error": job.error,
            "expired": job.expires_at < _utcnow(),
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    @staticmethod
    def _serialize_item(it: CloneJobItem) -> dict:
        return {
            "id": it.id, "job_id": it.job_id, "seq": it.seq, "kind": it.kind,
            "object_type": it.object_type, "object_name": it.object_name,
            "status": it.status, "error": it.error, "rows_copied": it.rows_copied,
            "execution_ms": it.execution_ms, "executed_at": it.executed_at,
        }

    # ------------------------------------------------------------------ #
    # Guard de alcance                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_scope(database: str, dialect: str, target) -> None:
        """
        Qué bases NO se pueden clonar por lo que SON, no por cómo se pidió.

        Faltaba por completo en este módulo, que es el que más superficie destructiva tiene:
        nada impedía apuntar un clon con ``clean_mode='objects'`` a la propia base de
        metadatos del gateway y dropear ``audit_log``/``servers``/``server_users`` — o sea el
        inventario, las credenciales pseudo-root cifradas de TODOS los servidores y la
        auditoría, que es el único control compensatorio que el repo declara. Se aplica a los
        DOS lados: el origen también, porque un clon lo LEE completo.

        Se reusa el guard de la consola SQL (``query_policy.is_gateway_metadata_target``) en
        vez de escribir un segundo criterio: resuelve ambos hosts a IPs e intersecta, así que
        registrar el servidor por su IP en lugar de su nombre no lo evade.

        ``target`` es OBLIGATORIO y sin default a propósito (mismo criterio que el export):
        con un default, un llamador nuevo se saltearía el guard en silencio, que es
        exactamente el modo de fallo que esto corrige.
        """
        validate_identifier(database, dialect, "base de datos", allow_existing=True)
        ensure_not_reserved_database(database, dialect)
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
                    "Esa base de datos es la propia base de metadatos del gateway: no se "
                    "puede usar como origen ni como destino de un clon."
                ),
                status_code=409,
                public_context={"code": cspec.CODE_SCOPE_NOT_ALLOWED},
                context={"database": database},
            )

    # ------------------------------------------------------------------ #
    # Crear plan                                                          #
    # ------------------------------------------------------------------ #
    def create_plan(self, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            src = self._resolve_source(
                session,
                database_id=data.get("source_database_id"),
                server_id=data.get("source_server_id"),
                database_name=data.get("source_database_name"),
            )
            tgt = self._resolve_target(
                session,
                server_id=data["target_server_id"],
                database_name=data["target_database_name"],
            )
        finally:
            session.close()

        target_mode = data["target_mode"]
        clean_mode = data.get("clean_mode", CLONE_CLEAN_NONE)
        include_data = bool(data.get("include_data", False))
        adopt_target = bool(data.get("adopt_target", False))
        selection = data.get("selection")
        # Traducción del atajo LEGACY al spec nuevo. El spec completo llega en el
        # ``preview``; acá solo se fija el punto de partida para que un cliente viejo (que
        # manda ``include_data`` y después previsualiza sin spec) obtenga exactamente el
        # plan de siempre.
        copy_intent = (
            CLONE_COPY_STRUCTURE_AND_DATA if include_data else CLONE_COPY_STRUCTURE_ONLY
        )
        # ``data_on_existing`` NO se persiste acá, y es deliberado: la columna significa
        # UNA sola cosa, "el operador lo eligió", que es la premisa de la que parte
        # ``validate_spec`` cuando la rechaza para toda intención que no sea 'data_only'.
        # Escribir en ella la derivación histórica hacía que un valor del SERVIDOR se leyera
        # como una elección del CLIENTE: cualquier preview con al menos un campo (y la SPA
        # manda siempre ``selection``) recibía un 422 clone.conflicting_options del que no
        # se podía salir. Lo EJECUTADO no cambia: ``_build_execution_plan`` cae a
        # ``cspec.legacy_upsert(...)`` cuando la columna es NULL.

        # Guarda: origen y destino no pueden ser la MISMA BD física.
        if src.server_id == tgt.server_id and src.database_name == tgt.database_name:
            raise AppHttpException(
                message="El origen y el destino no pueden ser la misma base de datos.",
                status_code=422,
                public_context={"code": cspec.CODE_SAME_DATABASE},
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )

        # Alcance: qué bases no se pueden tocar, en los DOS lados.
        self._validate_scope(src.database_name, src.engine, src.target)
        self._validate_scope(tgt.database_name, tgt.engine, tgt.target)

        # Existencia en vivo del origen y del destino.
        src_adapter = get_adapter(src.target)
        tgt_adapter = get_adapter(tgt.target)
        live_source = src_adapter.list_databases()
        if src.database_name not in live_source:
            raise AppHttpException(
                message=f"La BD origen '{src.database_name}' no existe en el servidor.",
                status_code=404,
                public_context={"code": cspec.CODE_SOURCE_NOT_FOUND},
                context={"server_id": src.server_id, "database": src.database_name},
            )
        target_exists = tgt.database_name in tgt_adapter.list_databases()
        if target_mode == CLONE_TARGET_NEW and target_exists:
            raise AppHttpException(
                message=f"La BD destino '{tgt.database_name}' ya existe. Usá target_mode='existing'.",
                status_code=422,
                public_context={"code": cspec.CODE_TARGET_ALREADY_EXISTS},
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )
        if target_mode == CLONE_TARGET_EXISTING and not target_exists:
            raise AppHttpException(
                message=f"La BD destino '{tgt.database_name}' no existe. Usá target_mode='new'.",
                status_code=404,
                public_context={"code": cspec.CODE_TARGET_NOT_FOUND},
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )
        if clean_mode != CLONE_CLEAN_NONE and target_mode == CLONE_TARGET_NEW:
            raise AppHttpException(
                message="clean_mode solo aplica a un destino existente (target_mode='existing').",
                status_code=422,
                public_context={"code": cspec.CODE_CONFLICTING_OPTIONS},
                context={"clean_mode": clean_mode, "target_mode": target_mode},
            )

        # Guarda de auto-adopt: solo clon COMPLETO desde un origen gestionado con blueprint.
        if adopt_target:
            if selection is not None:
                raise AppHttpException(
                    message="adopt_target solo es válido en un clon COMPLETO (sin selección parcial).",
                    status_code=422,
                    public_context={"code": cspec.CODE_ADOPT_REQUIRES_STRUCTURE},
                    context={},
                )
            if src.model_id is None:
                raise AppHttpException(
                    message="adopt_target requiere que el origen sea una BD gestionada con blueprint.",
                    status_code=422,
                    public_context={"code": cspec.CODE_ADOPT_REQUIRES_STRUCTURE},
                    context={"source_managed_id": src.managed_id},
                )
            # El owner del registro adoptado debe ser un ServerUser del servidor DESTINO.
            owner_id = data.get("adopt_owner_id")
            vsession = self._session()
            try:
                owner = vsession.get(ServerUser, owner_id) if owner_id else None
                if owner is None or owner.server_id != tgt.server_id:
                    raise AppHttpException(
                        message="adopt_owner_id debe ser un usuario del servidor destino.",
                        status_code=422,
                        public_context={"code": cspec.CODE_OWNER_INVALID},
                        context={"adopt_owner_id": owner_id, "target_server_id": tgt.server_id},
                    )
            finally:
                vsession.close()

        # Snapshot del origen (solo lectura) + fingerprint anti-TOCTOU.
        source_snap = self._source_snapshot(src.target, src.database_name)
        src_fp = _snapshot_fingerprint(source_snap)

        expires = _utcnow() + timedelta(hours=CLONE_TTL_HOURS)
        session = self._session()
        try:
            job = CloneJob(
                source_server_id=src.server_id,
                source_database_name=src.database_name,
                source_database_id=src.managed_id,
                source_engine=src.engine,
                target_server_id=tgt.server_id,
                target_database_name=tgt.database_name,
                target_database_id=tgt.managed_id,
                target_engine=tgt.engine,
                include_data=include_data,
                clean_mode=clean_mode,
                target_mode=target_mode,
                adopt_target=adopt_target,
                adopt_owner_id=data.get("adopt_owner_id"),
                selection=json.dumps(selection) if selection is not None else None,
                # Predicado EXPLÍCITO, no inferido de ``selection is None``: con la selección
                # declarativa resuelta a lista explícita, esa inferencia dejaba
                # ``will_adopt`` en False para siempre y el auto-adopt se apagaba solo.
                is_full_clone=selection is None,
                copy_intent=copy_intent,
                source_fingerprint=src_fp,
                expires_at=expires,
                status=CLONE_STATUS_PENDING,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            result = self._serialize_summary(job)
            job_id = job.id
        finally:
            session.close()

        audit.record(
            "clone.plan",
            admin=admin,
            target_type="managed_database",
            target_id=tgt.managed_id,
            server_id=tgt.server_id,
            touched_engine=True,  # se snapshoteó el origen (solo lectura)
            detail=(
                f"plan de clon {job_id}: {src.server_id}/{src.database_name} → "
                f"{tgt.server_id}/{tgt.database_name} "
                f"(copy={copy_intent}, mode={target_mode}, clean={clean_mode})"
            ),
        )
        return result

    # ------------------------------------------------------------------ #
    # Lectura                                                             #
    # ------------------------------------------------------------------ #
    def get_plan(self, job_id: int) -> dict:
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            return self._serialize_summary(job)
        finally:
            session.close()

    def _load_side_targets(self, job: CloneJob) -> tuple[ServerTarget, ServerTarget]:
        """Reconstruye los ServerTarget de origen y destino desde los servidores del job."""
        session = self._session()
        try:
            src_server = get_server_or_404(session, job.source_server_id)
            tgt_server = get_server_or_404(session, job.target_server_id)
            return build_target(src_server), build_target(tgt_server)
        finally:
            session.close()

    def list_objects(self, job_id: int, *, with_estimates: bool = False) -> dict:
        """
        Inventario en vivo del origen + portabilidad + grafo de dependencias.

        ``with_estimates`` es opt-in porque ``list_table_stats`` consulta el catálogo una vez
        por tabla: en una BD con doscientas, pedirlo siempre convertiría este endpoint (10/min,
        con el timeout interactivo por sentencia) en el más caro del módulo. Precedente:
        ``?include_data_stats=true`` del endpoint de snapshot.
        """
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            include_data = with_estimates or bool(job.include_data)
            source_db = job.source_database_name
            src_target, _ = self._load_side_targets(job)
            tgt_engine = job.target_engine
        finally:
            session.close()
        snap = get_adapter(src_target).structural_snapshot(source_db)
        stats: dict = {}
        if include_data:
            # Estimaciones del CATÁLOGO (no un COUNT: contar recorrería cada tabla del
            # origen, que es justo lo que un catálogo no debe hacer). Best-effort: si el
            # motor las rechaza, el inventario sale igual sin ellas.
            try:
                stats = {
                    st.table: st
                    for st in get_adapter(src_target).list_table_stats(source_db)
                }
            except AppHttpException:
                stats = {}
        return self._build_inventory(
            snap, tgt_engine, include_data=include_data, stats=stats
        )

    def resolve_selection(self, job_id: int, selection: list[dict]) -> dict:
        """Cierre de dependencias (autoritativo) + advisory para una selección."""
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            src_target, _ = self._load_side_targets(job)
        finally:
            session.close()
        snap = get_adapter(src_target).structural_snapshot(job.source_database_name)
        refs = [cdeps.ObjectRef(object_type=s["object_type"], name=s["name"]) for s in selection]
        res = cdeps.resolve_closure(snap, refs)
        return res.model_dump()

    # ------------------------------------------------------------------ #
    # Construcción del plan de ejecución (compartido por preview y runner) #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _empty_snapshot(engine: str, database: str) -> SchemaSnapshot:
        return SchemaSnapshot(database=database, source_engine=engine)

    def _closure_keys(self, snap: SchemaSnapshot, selection: list[dict] | None) -> set[tuple[str, str]] | None:
        """Conjunto (object_type, name) a clonar; None = todo el snapshot."""
        if selection is None:
            return None
        refs = [cdeps.ObjectRef(object_type=s["object_type"], name=s["name"]) for s in selection]
        res = cdeps.resolve_closure(snap, refs)
        return {(r.object_type, r.name) for r in res.closure}

    def _filter_snapshot(self, snap: SchemaSnapshot, keys: set[tuple[str, str]] | None) -> SchemaSnapshot:
        """Devuelve un snapshot con solo los objetos en ``keys`` (None = sin filtrar)."""
        if keys is None:
            return snap
        return SchemaSnapshot(
            database=snap.database, source_engine=snap.source_engine, captured_at=snap.captured_at,
            tables=[t for t in snap.tables if ("table", t.table) in keys],
            views=[v for v in snap.views
                   if (("materialized_view" if v.is_materialized else "view"), v.name) in keys],
            routines=[r for r in snap.routines if ("routine", r.name) in keys],
            triggers=[tg for tg in snap.triggers if ("trigger", tg.name) in keys],
            sequences=[s for s in snap.sequences if ("sequence", s.name) in keys],
            enum_types=[e for e in snap.enum_types if ("enum_type", e.name) in keys],
            extensions=[x for x in snap.extensions if ("extension", x.name) in keys],
            events=[ev for ev in snap.events if ("event", ev.name) in keys],
        )

    def _build_execution_plan(
        self,
        job: CloneJob,
        source_snap: SchemaSnapshot,
        target_snap: SchemaSnapshot | None,
        *,
        tgt_target: ServerTarget,
        src_target: ServerTarget | None = None,
        with_estimates: bool = False,
    ) -> _ExecutionPlan:
        """
        Arma el plan determinista: sentencias de limpieza (si aplica), sentencias de
        estructura (CREATE en el dialecto destino) y specs de datos. Reutiliza el pipeline
        diff+render: estructura = diff(origen_filtrado vs vacío) → 'new'; limpieza objeto
        por objeto = diff(vacío vs destino) → 'dropped'.

        ``with_estimates`` solo se activa en los caminos de LECTURA (``objects``/``preview``):
        la estimación de filas es informativa, cuesta una consulta de catálogo por tabla y no
        entra al ``confirm_token`` — si entrara, un ``ANALYZE`` de fondo entre el preview y el
        execute invalidaría el token sin que el plan haya cambiado. El worker no la pide.
        """
        selection = json.loads(job.selection) if job.selection else None
        data_selection = json.loads(job.data_selection) if job.data_selection else None
        intent = job.copy_intent or CLONE_COPY_STRUCTURE_ONLY
        entity_ddl = cspec.entity_ddl_for(cspec.CopyIntent(intent))
        wants_data = intent in (CLONE_COPY_STRUCTURE_AND_DATA, CLONE_COPY_DATA_ONLY)
        tgt_engine = job.target_engine
        tgt_adapter = get_adapter(tgt_target)

        keys = self._closure_keys(source_snap, selection)
        filtered = self._filter_snapshot(source_snap, keys)

        # --- Estructura: diff(origen filtrado vs destino vacío) → todo 'new' ---------- #
        # Con ``entity_ddl=NONE`` (copy='data_only') NO se emite una sola sentencia. El
        # snapshot filtrado se sigue calculando: lo necesitan las specs de datos, el orden
        # topológico y el guard de compatibilidad.
        structure: list[_StructStmt] = []
        skipped: list[dict] = []
        if entity_ddl != espec.EntityDdl.NONE:
            empty_tgt = self._empty_snapshot(tgt_engine, job.target_database_name)
            struct_diff = diff_snapshots(filtered, empty_tgt)
            rendered = tgt_adapter.render_diff(struct_diff)
            skipped_names: set[str] = set()
            for r in rendered:
                portable, reason = self._portability(
                    r.object_type, source_snap.source_engine, tgt_engine
                )
                if portable:
                    sql = r.sql
                    if r.object_type in _BODY_TYPES:
                        sql = self._requalify_body(
                            sql, source_snap.database, job.target_database_name, tgt_engine
                        )
                    structure.append(_StructStmt("structure", r.object_type, r.object_name, sql))
                elif r.object_name not in skipped_names:
                    skipped_names.add(r.object_name)
                    skipped.append({
                        "object_type": r.object_type, "name": r.object_name,
                        "portable": False, "portability_reason": reason, "row_estimate": None,
                    })

        # --- Limpieza objeto por objeto (solo clean_mode='objects') ------------------- #
        # 'drop_database' NO produce sentencias aquí: es una operación a nivel servidor que
        # el runner ejecuta con adapter.drop_database/create_database desde los campos del job.
        clean: list[_StructStmt] = []
        if job.clean_mode == CLONE_CLEAN_OBJECTS and target_snap is not None:
            empty_src = self._empty_snapshot(tgt_engine, job.target_database_name)
            drop_diff = diff_snapshots(empty_src, target_snap)
            for r in tgt_adapter.render_diff(drop_diff):
                clean.append(_StructStmt("clean", r.object_type, r.object_name, r.sql))

        # --- Datos ------------------------------------------------------------------- #
        data_specs: list[_DataSpec] = []
        notices: list[dict] = []
        stats: dict = {}
        if wants_data and with_estimates and src_target is not None:
            # Estimaciones del CATÁLOGO (no un COUNT): informan el tamaño de lo que se va a
            # copiar, que hasta ahora el contrato prometía y nunca poblaba.
            try:
                stats = {
                    st.table: st
                    for st in get_adapter(src_target).list_table_stats(source_snap.database)
                }
            except AppHttpException:
                stats = {}  # informativo: su ausencia no invalida el plan
        if wants_data:
            on_existing = job.data_on_existing
            upsert = (
                on_existing == cspec.DataOnExisting.upsert.value
                if on_existing
                else cspec.legacy_upsert(job.target_mode, job.clean_mode)
            )
            if data_selection is not None:
                # Eje de datos PROPIO (ya con su cierre por FK resuelto en el preview).
                table_names: set[str] | None = set(data_selection)
                order_source = self._filter_snapshot(
                    source_snap, {("table", n) for n in table_names}
                )
            else:
                # Camino histórico: las tablas con datos se derivan del cierre de estructura.
                table_names = (
                    {name for (ot, name) in (keys or set()) if ot == "table"} if keys else None
                )
                order_source = filtered
            ordered = self._data_table_order(order_source)
            for t in ordered:
                if table_names is not None and t.table not in table_names:
                    continue
                # Datos solo si la tabla es portable (misma familia siempre; cross-family: sí).
                portable, _ = self._portability("table", source_snap.source_engine, tgt_engine)
                if not portable:
                    continue
                # Columnas GENERATED (STORED/VIRTUAL) se excluyen: el motor las recalcula
                # solo. Escribirles un valor explicito da un warning (MySQL 1906) que en
                # sql_mode estricto se promueve a error y aborta la tabla completa.
                st = stats.get(t.table)
                data_specs.append(_DataSpec(
                    table=t.table,
                    columns=[c.name for c in t.columns if c.computed is None],
                    primary_key=list(t.primary_key),
                    upsert=upsert,
                    row_estimate=st.estimated_rows if st is not None else None,
                    row_estimate_known=st.estimated_rows_known if st is not None else True,
                    has_primary_key=(
                        st.has_primary_key if st is not None else bool(t.primary_key)
                    ),
                    # No se filtra el PK de esta cuenta a propósito: la decisión de staging
                    # es un ``or`` con la PK, así que "el único unique ES el PK" ya está
                    # cubierto y filtrarlo solo agregaría una comparación de conjuntos.
                    has_unique_key=(
                        any(ix.unique for ix in t.indexes) or bool(t.unique_constraints)
                    ),
                ))

        # --- Guard de compatibilidad del DESTINO -------------------------------------- #
        # Solo cuando este job NO crea la estructura (copy='data_only'): si la crea, las
        # tablas nacen con el esquema del origen y no hay nada que reconciliar. Acá el motor
        # NO es la red de seguridad en la familia MySQL (``LOAD DATA LOCAL`` degrada los
        # errores de tipo a warnings), así que este guard es la única defensa.
        issues: list[cspec.CompatIssue] = []
        if data_specs and entity_ddl == espec.EntityDdl.NONE:
            issues = cspec.data_compat_issues(
                source=source_snap,
                target=target_snap,
                data_columns={d.table: d.columns for d in data_specs},
                source_engine=source_snap.source_engine,
                target_engine=tgt_engine,
            )
            notices.extend(self._trigger_notices(target_snap, data_specs, tgt_engine))

        for d in data_specs:
            if d.upsert and d.has_primary_key is False:
                notices.append({
                    "code": cspec.WARN_UPSERT_WITHOUT_PRIMARY_KEY,
                    "message": (
                        f"La tabla '{d.table}' no tiene clave primaria: el modo 'upsert' "
                        f"degrada a INSERT simple, así que volver a ejecutar este job "
                        f"duplicaría sus filas."
                    ),
                    "severity": "warning",
                    "detail": {"table": d.table},
                })
        notices.extend(self._charset_notices(job, source_snap))
        notices.extend(self._owner_notices(job))

        legacy_warnings = (
            self._autoincrement_pk_warnings(filtered, tgt_engine)
            if entity_ddl != espec.EntityDdl.NONE
            else []
        ) + self._external_fk_warnings(tgt_adapter, job)
        # ``warnings`` conserva su tipo (lista de strings) porque el cliente descarta la
        # respuesta ENTERA si el schema no valida; ``notices`` es la versión con código del
        # mismo contenido.
        for w in legacy_warnings:
            notices.append({
                "code": cspec.WARN_AUTOINCREMENT_KEY_ADDED
                if "AUTO_INCREMENT" in w
                else cspec.WARN_EXTERNAL_FK_DEPENDENTS,
                "message": w,
                "severity": "warning",
                "detail": {},
            })
        for issue in issues:
            if not issue.blocking:
                notices.append({
                    "code": cspec.WARN_SCHEMA_DIFFERENCE,
                    "message": self._issue_message(issue),
                    "severity": "warning",
                    "detail": {
                        "table": issue.table, "column": issue.column,
                        "reason": issue.reason, **issue.detail,
                    },
                })

        return _ExecutionPlan(
            clean_statements=clean,
            structure_statements=structure,
            data_specs=data_specs,
            skipped=skipped,
            will_adopt=job.adopt_target and job.is_full_clone and job.source_database_id is not None,
            table_order=[t.table for t in self._data_table_order(filtered)],
            warnings=legacy_warnings + [
                self._issue_message(i) for i in issues if not i.blocking
            ],
            blocking_issues=[
                {
                    "table": i.table, "reason": i.reason, "blocking": True,
                    "column": i.column, "detail": i.detail,
                }
                for i in issues
                if i.blocking
            ],
            notices=notices,
        )

    # ------------------------------------------------------------------ #
    # Avisos derivados del plan                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _issue_message(issue) -> str:
        """Texto legible de una incompatibilidad, desde vocabulario CERRADO."""
        where = f"{issue.table}.{issue.column}" if issue.column else issue.table
        texts = {
            cspec.REASON_TARGET_NOT_INSPECTED: "no se pudo inspeccionar el esquema del destino",
            cspec.REASON_TABLE_MISSING: "la tabla no existe en el destino",
            cspec.REASON_TABLE_IS_VIEW: "en el destino ese nombre es una vista, no una tabla",
            cspec.REASON_COLUMN_MISSING: "la columna no existe en el destino",
            cspec.REASON_TARGET_NOT_NULL_NO_DEFAULT: (
                "el destino tiene esa columna NOT NULL y sin default, y el origen no la aporta"
            ),
            cspec.REASON_TARGET_GENERATED: "en el destino esa columna es GENERATED (no admite escritura)",
            cspec.REASON_TARGET_IDENTITY_ALWAYS: (
                "en el destino esa columna es IDENTITY ALWAYS (no admite un valor explícito)"
            ),
            cspec.REASON_TYPE_NARROWING: (
                "el tipo del destino es más chico que el del origen: los valores se truncarían"
            ),
            cspec.REASON_UNSIGNED_TO_SIGNED: (
                "el origen es UNSIGNED y el destino no: los valores altos no entran"
            ),
            cspec.REASON_COLLATION_ON_KEY: (
                "la collation difiere en una columna de clave: dos filas del origen pueden "
                "colapsar en una sola en el destino"
            ),
            cspec.REASON_COLLATION_DIFFERS: "la collation de la columna difiere",
            cspec.REASON_TARGET_UNIQUE_EXTRA: (
                "el destino tiene una clave única que el origen no: puede rechazar o "
                "descartar filas"
            ),
            cspec.REASON_TARGET_CHECK_EXTRA: "el destino tiene un CHECK que el origen no",
            cspec.REASON_TARGET_FK_OUTSIDE_SELECTION: (
                "el destino tiene una FK hacia una tabla que este job no puebla: quedarían "
                "filas huérfanas (la copia corre con las FKs desactivadas y el motor no las "
                "revalida)"
            ),
            cspec.REASON_TYPE_DIFFERS: "el tipo de la columna difiere",
            cspec.REASON_TYPES_NOT_VERIFIED: (
                "origen y destino son de familias distintas: la fidelidad de los tipos no se "
                "verificó"
            ),
            cspec.REASON_TYPE_NOT_LOADABLE: (
                "el tipo del destino no se puede alimentar desde la otra familia de motores"
            ),
            cspec.REASON_SOURCE_NULLABLE_TARGET_NOT_NULL: (
                "el origen admite NULL y el destino no: una fila con NULL fallaría"
            ),
        }
        return f"{where}: {texts.get(issue.reason, issue.reason)}"

    @staticmethod
    def _trigger_notices(target_snap, data_specs, tgt_engine) -> list[dict]:
        """
        Triggers del DESTINO que van a dispararse durante la copia.

        Toda la maquinaria de ``_POST_DATA_BODY_TYPES`` existe porque un trigger de INSERT
        que puebla otra tabla, disparado durante la copia, duplica filas (ER_DUP_ENTRY 1062
        en una pivote). Pero esa defensa solo cubre los triggers que ESTE job crea: los
        difiere hasta después de los datos. En 'data_only' no se crea ninguno — los del
        destino ya están, vivos.

        Reparto por motor, y el aviso se emite en los DOS porque la garantía de PostgreSQL no
        es firme: ``session_replication_role='replica'`` los apaga, pero es un ``SET`` de
        superusuario cuyo error se traga (best-effort en ``data_copy``), así que un
        pseudo-root sin ese privilegio deja los triggers activos sin que nada falle. Prometer
        que allá no hay nada que advertir sería fail-open.
        """
        if target_snap is None:
            return []
        tables = {d.table for d in data_specs}
        affected: dict[str, list[str]] = {}
        for tg in target_snap.triggers:
            if tg.table in tables:
                affected.setdefault(tg.table, []).append(tg.name)
        if not affected:
            return []
        listed = ", ".join(
            f"{t} ({', '.join(sorted(names))})" for t, names in sorted(affected.items())
        )
        if tgt_engine in ("mysql", "mariadb"):
            msg = (
                f"El destino tiene triggers sobre tablas que van a recibir filas y VAN A "
                f"DISPARARSE durante la copia: {listed}. En MySQL/MariaDB no hay forma "
                f"portable de desactivarlos (FOREIGN_KEY_CHECKS=0 no los apaga), así que si "
                f"alguno escribe en otra tabla, la va a modificar."
            )
        else:
            msg = (
                f"El destino tiene triggers sobre tablas que van a recibir filas: {listed}. "
                f"El gateway intenta desactivarlos con session_replication_role='replica', "
                f"pero eso requiere superusuario: si el motor lo rechaza, los triggers "
                f"disparan igual."
            )
        return [{
            "code": cspec.WARN_TARGET_TRIGGERS_WILL_FIRE,
            "message": msg,
            "severity": "warning",
            "detail": {"tables": {t: sorted(n) for t, n in affected.items()}},
        }]

    @staticmethod
    def _charset_notices(job: CloneJob, source_snap: SchemaSnapshot) -> list[dict]:
        """
        Consecuencia de elegir un charset/collation distinto del origen.

        Las tablas que se crean SIN collation explícita heredan el default de la BD, así que
        un diff posterior origen↔destino va a marcar diferencias. Y el síntoma es ASIMÉTRICO:
        en MySQL/MariaDB ``COLLATION_NAME`` trae siempre la collation física resuelta, así
        que el diff grita en toda columna textual; en PostgreSQL es NULL cuando se hereda, así
        que el diff se queda CALLADO mientras el orden real de los índices cambió. El silencio
        es el caso peor, y por eso se avisa antes.
        """
        if not job.target_charset and not job.target_collation:
            return []
        src_cs = getattr(source_snap, "db_charset", None)
        src_co = getattr(source_snap, "db_collation", None)
        if (job.target_charset or src_cs) == src_cs and (job.target_collation or src_co) == src_co:
            return []
        return [{
            "code": cspec.WARN_CHARSET_DIFFERS_FROM_SOURCE,
            "message": (
                f"La BD destino se creará con "
                f"{job.target_charset or '(default)'}/{job.target_collation or '(default)'}, "
                f"distinto del origen ({src_cs or '?'}/{src_co or '?'}). Las tablas que se "
                f"creen sin collation explícita heredarán el default del destino, así que un "
                f"diff posterior entre las dos bases va a reportar diferencias — y en "
                f"PostgreSQL puede NO reportarlas aunque el orden de los índices haya "
                f"cambiado."
            ),
            "severity": "warning",
            "detail": {
                "target_charset": job.target_charset,
                "target_collation": job.target_collation,
                "source_charset": src_cs,
                "source_collation": src_co,
            },
        }]

    @staticmethod
    def _owner_notices(job: CloneJob) -> list[dict]:
        """
        Qué logra REALMENTE el owner en PostgreSQL, dicho sin adornos.

        ``CREATE DATABASE … OWNER x`` fija el dueño de la BASE y nada más: las tablas, vistas
        y secuencias las crea la conexión pseudo-root y quedan con SU propiedad, así que el
        dueño pedido no puede ``ALTER`` ni ``DROP`` sus propios objetos. Reasignarlos requiere
        ``SET ROLE``/``REASSIGN OWNED``, que es trabajo aparte. Prometer "la copia queda a
        nombre de x" sin esto sería mentir.
        """
        if not job.target_owner:
            return []
        return [{
            "code": cspec.WARN_OWNER_OBJECTS_NOT_REASSIGNED,
            "message": (
                f"La BD destino se creará con OWNER '{job.target_owner}', pero los objetos "
                f"(tablas, vistas, secuencias) los crea la credencial administrativa del "
                f"gateway y quedan con SU propiedad: '{job.target_owner}' será dueño de la "
                f"base, no de su contenido."
            ),
            "severity": "info",
            "detail": {"owner": job.target_owner},
        }]

    @staticmethod
    def _external_fk_warnings(tgt_adapter, job: CloneJob) -> list[str]:
        """
        Si la limpieza va a DROPear objetos del destino (``clean_mode='objects'`` o
        ``'drop_database'`` sobre un destino EXISTENTE), consulta si alguna tabla de OTRA
        base de datos del mismo servidor tiene una FK hacia el destino — invisible al
        snapshot de una sola BD, y el candidato más probable ante un
        ``(1451, 'Cannot delete or update a parent row...')`` en un DROP aislado (ver
        ``execute_adhoc(disable_fk_checks=True)``, que resuelve el bloqueo pero no informa
        la causa). Solo MySQL/MariaDB devuelven algo (PostgreSQL no soporta FKs
        cross-database). Best-effort: si la consulta falla, no bloquea el preview.
        """
        if job.target_mode != CLONE_TARGET_EXISTING:
            return []
        if job.clean_mode not in (CLONE_CLEAN_OBJECTS, CLONE_CLEAN_DROP_DATABASE):
            return []
        try:
            deps = tgt_adapter.external_fk_dependents(job.target_database_name)
        except AppHttpException:
            return []
        if not deps:
            return []
        examples = ", ".join(
            f"`{d.schema_name}`.`{d.table}`.`{d.column}` → `{d.referenced_table}`.`{d.referenced_column}`"
            for d in deps[:5]
        )
        more = f" (+{len(deps) - 5} más)" if len(deps) > 5 else ""
        return [
            f"Hay {len(deps)} columna(s) en OTRA(S) base(s) de datos del servidor con una "
            f"FK hacia `{job.target_database_name}` (invisible al snapshot de esta BD): "
            f"{examples}{more}. La limpieza desactiva temporalmente el chequeo de FKs para "
            "completarse igual, pero esas referencias externas pueden quedar apuntando a "
            "datos ya reemplazados — revisar antes de continuar si es crítico para esas "
            "otras bases."
        ]

    @staticmethod
    def _autoincrement_pk_warnings(snap: SchemaSnapshot, tgt_engine: str) -> list[str]:
        """
        MySQL/MariaDB (InnoDB) exige que la columna AUTO_INCREMENT sea la primera
        columna de alguna clave definida en la MISMA sentencia CREATE TABLE. Si el
        origen trae una PK compuesta heredada donde el autoincrement no la encabeza,
        el renderer agrega automáticamente una KEY de apoyo (ver
        ``MySQLAdapter._render_create_table``) — se avisa aquí para que el operador
        lo vea en el preview, no solo leyendo el DDL en ``GET .../items``.
        """
        if tgt_engine not in _MYSQL_FAMILY:
            return []
        warnings: list[str] = []
        for t in snap.tables:
            auto_col = next((c.name for c in t.columns if c.autoincrement), None)
            if not auto_col:
                continue
            leads_pk = bool(t.primary_key) and t.primary_key[0] == auto_col
            leads_unique = any(
                uc.columns and uc.columns[0] == auto_col for uc in t.unique_constraints
            )
            if not leads_pk and not leads_unique:
                warnings.append(
                    f"La tabla `{t.table}` tiene una columna AUTO_INCREMENT (`{auto_col}`) "
                    "que no es la primera columna de la PRIMARY KEY de origen; se agregará "
                    f"automáticamente un índice de apoyo (KEY (`{auto_col}`)) en el destino "
                    "para que MySQL/MariaDB acepte la creación."
                )
        return warnings

    @staticmethod
    def _resync_postgres_identity_sequences(
        tgt_target: ServerTarget,
        target_db: str,
        source_snap: SchemaSnapshot,
        results: list,
    ) -> None:
        """
        MySQL/InnoDB ajusta AUTO_INCREMENT solo al insertar un valor explícito mayor al
        contador actual (no requiere ningún paso nuestro). PostgreSQL NO hace eso: ni un
        ``INSERT ... OVERRIDING SYSTEM VALUE`` ni un ``COPY`` (que es como este módulo
        escribe los datos) avanzan la secuencia asociada a una columna
        ``GENERATED {ALWAYS|BY DEFAULT} AS IDENTITY`` — queda en su valor inicial. El
        primer ``INSERT`` real de la aplicación que dependa del default/identity para
        generar un ID coincidiría con una fila ya clonada → choque de PK. Confirmado
        contra la documentación oficial de PostgreSQL (``pg_get_serial_sequence`` funciona
        también para columnas IDENTITY, no solo ``serial``).

        Se apoya en el snapshot del ORIGEN (mismo criterio que ``_autoincrement_pk_warnings``)
        para saber qué columnas son identity — asume que el destino las espeja (cierto en
        ``target_mode=new``; aproximación razonable en ``target_mode=existing``).

        Best-effort por tabla/columna: un fallo aquí NO revierte los datos ya copiados
        (que son correctos) ni marca el job como fallido — solo se loguea. Solo corre para
        tablas con ``status='applied'`` y al menos una fila copiada (una tabla vacía no
        mueve la secuencia, y ``MAX(col)`` sobre 0 filas sería ``NULL``).
        """
        tables_by_name = {t.table: t for t in source_snap.tables}
        applied_tables = {r.table for r in results if r.status == "applied" and r.rows_copied > 0}
        if not applied_tables:
            return
        with database_connection(tgt_target, target_db) as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            for table_name in applied_tables:
                table = tables_by_name.get(table_name)
                if table is None:
                    continue
                for col in table.columns:
                    if col.identity is None:
                        continue
                    col_q = quote_identifier(
                        validate_identifier(col.name, "postgresql", "columna", allow_existing=True),
                        "postgresql",
                    )
                    table_q = quote_identifier(
                        validate_identifier(table.table, "postgresql", "tabla", allow_existing=True),
                        "postgresql",
                    )
                    try:
                        conn.execute(
                            text(
                                "SELECT setval(pg_get_serial_sequence(:t, :c), "
                                f"(SELECT MAX({col_q}) FROM {table_q}), true)"
                            ),
                            {"t": table.table, "c": col.name},
                        )
                    except SQLAlchemyError:
                        logger.warning(
                            "Clon: no se pudo resincronizar la secuencia de %s.%s "
                            "(los datos ya copiados no se ven afectados).",
                            table_name, col.name,
                        )

    @staticmethod
    def _requalify_body(sql: str, source_db: str, target_db: str, tgt_engine: str) -> str:
        """
        Re-califica el esquema en el cuerpo de un objeto (vista/rutina/trigger/evento).

        Delega en ``sql_dialect.requalify_body_schema`` (fuente única de verdad, compartida
        con schema-comparison). MySQL/MariaDB inyectan el esquema ORIGEN en las referencias
        del cuerpo; sin reescribir origen→destino el objeto seguiría leyendo de la BD ORIGEN
        (fuga cross-database / clon roto). Solo aplica a la familia MySQL/MariaDB.
        """
        return requalify_body_schema(sql, source_db, target_db, tgt_engine)

    @staticmethod
    def _data_table_order(snap: SchemaSnapshot):
        """Tablas ordenadas topológicamente (padre antes que hijo) para insertar datos."""
        from app.services.db_admin.schema_diff import _table_dep_order
        by_name = {t.table: t for t in snap.tables}
        rank = _table_dep_order(list(by_name), by_name)
        return sorted(snap.tables, key=lambda t: (rank.get(t.table, 0), t.table))

    # ------------------------------------------------------------------ #
    # Snapshots que el plan necesita                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _needs_target_snapshot(job: CloneJob) -> bool:
        """
        ¿Hay que inspeccionar el destino para armar el plan?

        Dos casos, y solo dos: ``clean_mode='objects'`` (hay que rendear los DROP de lo que
        el destino tenga) y ``copy='data_only'`` (el guard de compatibilidad compara contra el
        esquema real del destino). La condición vive ACÁ y no repetida en cada llamador: el
        plan se arma en TRES lugares (preview, execute y el worker) y el que quedara sin el
        snapshot armaría un plan sin guard, en silencio.
        """
        if job.target_mode != CLONE_TARGET_EXISTING:
            return False
        if job.clean_mode == CLONE_CLEAN_OBJECTS:
            return True
        return cspec.entity_ddl_for(
            cspec.CopyIntent(job.copy_intent or CLONE_COPY_STRUCTURE_ONLY)
        ) == espec.EntityDdl.NONE

    def _snapshots_for(
        self, job: CloneJob, src_target: ServerTarget, tgt_target: ServerTarget
    ) -> tuple[SchemaSnapshot, SchemaSnapshot | None]:
        """Snapshot del origen (siempre) y del destino (si el plan depende de él)."""
        source_snap = self._source_snapshot(src_target, job.source_database_name)
        target_snap = None
        if self._needs_target_snapshot(job):
            target_snap = get_adapter(tgt_target).structural_snapshot(job.target_database_name)
        return source_snap, target_snap

    # ------------------------------------------------------------------ #
    # Token de confirmación                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def clone_execution_token(
        target_ref: str, target_engine: str, plan: _ExecutionPlan, *, clean_mode: str,
        target_mode: str, copy_intent: str, on_existing: str | None,
        target_charset: str | None, target_collation: str | None, target_owner: str | None,
    ) -> str:
        """
        SHA256 del plan EXACTO.

        Los ejes del CONTENEDOR (charset, collation, owner) y la intención entran
        explícitamente: no están en el texto de ninguna sentencia —el ``CREATE DATABASE`` lo
        arma el worker desde los campos del job—, así que sin esto se podría previsualizar con
        un charset y ejecutar con otro. Las ESTIMACIONES de filas quedan afuera a propósito:
        un ``ANALYZE`` de fondo entre el preview y el execute invalidaría el token sin que el
        plan haya cambiado.
        """
        parts: list[str] = [
            str(target_ref), str(target_engine), clean_mode, target_mode, str(copy_intent),
            str(on_existing), str(target_charset), str(target_collation), str(target_owner),
        ]
        for s in plan.clean_statements:
            parts.append(f"clean:{s.object_type}:{s.sql}")
        for s in plan.structure_statements:
            parts.append(f"struct:{s.object_type}:{s.sql}")
        for d in plan.data_specs:
            parts.append(f"data:{d.table}:{','.join(d.columns)}:{int(d.upsert)}")
        parts.append(f"adopt:{int(plan.will_adopt)}")
        blob = "\x1f".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _token_for(self, job: CloneJob, plan: _ExecutionPlan) -> str:
        """``clone_execution_token`` con todos los ejes tomados del job (una sola fuente)."""
        return self.clone_execution_token(
            f"{job.target_server_id}:{job.target_database_name}",
            job.target_engine,
            plan,
            clean_mode=job.clean_mode,
            target_mode=job.target_mode,
            copy_intent=job.copy_intent or CLONE_COPY_STRUCTURE_ONLY,
            on_existing=job.data_on_existing,
            target_charset=job.target_charset,
            target_collation=job.target_collation,
            target_owner=job.target_owner,
        )

    # ------------------------------------------------------------------ #
    # Preview: acá se CONGELA el spec                                      #
    # ------------------------------------------------------------------ #
    def _apply_spec(self, session, job: CloneJob, spec: dict, sent: set[str]) -> None:
        """
        Persiste en el job los ejes que el cliente MANDÓ, valida su coherencia y resuelve la
        selección declarativa contra el catálogo del origen.

        ``sent`` son los campos presentes en el request (``model_fields_set``), no los que
        tienen valor: un campo ausente deja lo que el plan ya tenía. Antes ``preview``
        persistía ``selection = None`` en cada llamada, así que un ``POST /preview {}``
        descartaba la selección que el operador había armado y devolvía —con token válido— el
        plan de un clon COMPLETO.
        """
        if "copy_intent" in sent and spec.get("copy_intent") is not None:
            job.copy_intent = str(spec["copy_intent"])
        intent = cspec.CopyIntent(job.copy_intent or CLONE_COPY_STRUCTURE_ONLY)

        # --- Selección de estructura: refs exactas o declarativa ------------------- #
        source_snap = None
        if "selection" in sent:
            sel = spec.get("selection")
            job.selection = json.dumps(sel) if sel is not None else None
            job.is_full_clone = sel is None
        elif "structure" in sent and spec.get("structure") is not None:
            st = spec["structure"]
            source_snap = self._source_snapshot(
                build_target(get_server_or_404(session, job.source_server_id)),
                job.source_database_name,
            )
            catalog = [
                espec.CatalogObject(object_type=ot, name=name)
                for ot, name in self._iter_objects(source_snap)
            ]
            resolved = espec.resolve_selection(
                catalog,
                espec.Selection(
                    mode=espec.SelectionMode(st.get("mode", "all")),
                    types=tuple(st.get("types") or ()),
                    names=tuple(st.get("names") or ()),
                    include_patterns=tuple(st.get("include_patterns") or ()),
                    exclude_patterns=tuple(st.get("exclude_patterns") or ()),
                ),
            )
            self._reject_unknown_names(resolved, st.get("mode", "all"), "structure")
            full = (
                st.get("mode", "all") == "all"
                and not st.get("names")
                and not st.get("include_patterns")
                and not st.get("exclude_patterns")
            )
            job.selection = None if full else json.dumps(
                [{"object_type": o.object_type, "name": o.name} for o in resolved.objects]
            )
            job.is_full_clone = full

        # --- Selección de datos ---------------------------------------------------- #
        data = spec.get("data") if "data" in sent else None
        data_mode = "none"
        if data is not None:
            data_mode = data.get("mode", "none")
            if data.get("on_existing") is not None:
                job.data_on_existing = str(data["on_existing"])
            if data_mode == "none":
                job.data_selection = None
                job.data_on_existing = None
            else:
                if source_snap is None:
                    source_snap = self._source_snapshot(
                        build_target(get_server_or_404(session, job.source_server_id)),
                        job.source_database_name,
                    )
                tables = espec.resolve_selection(
                    [
                        espec.CatalogObject(object_type="table", name=t.table)
                        for t in source_snap.tables
                    ],
                    espec.Selection(
                        mode=espec.DataSelectionMode(data_mode),
                        types=("table",),
                        names=tuple(data.get("names") or ()),
                        include_patterns=tuple(data.get("include_patterns") or ()),
                        exclude_patterns=tuple(data.get("exclude_patterns") or ()),
                    ),
                )
                self._reject_unknown_names(tables, data_mode, "data")
                # CIERRE POR FK. Hasta ahora el conjunto de tablas con datos se DERIVABA del
                # cierre autoritativo de la estructura, así que copiar 'orders' arrastraba
                # 'customers' por construcción. Con el eje de datos independiente esa
                # invariante desaparecía, y la fase de datos corre con las FKs desactivadas y
                # el motor NUNCA las revalida: el resultado serían filas huérfanas
                # permanentes, sin un solo error.
                closure = cdeps.resolve_closure(
                    source_snap,
                    [
                        cdeps.ObjectRef(object_type="table", name=o.name)
                        for o in tables.objects
                    ],
                )
                names = [r.name for r in closure.closure if r.object_type == "table"]
                job.data_selection = json.dumps(names)
        elif job.data_selection is None and intent is cspec.CopyIntent.data_only:
            # ``data_only`` sin bloque de datos: no hay nada que copiar y el plan quedaría
            # vacío. Lo reporta ``validate_spec`` más abajo con su código.
            data_mode = "none"
        elif job.data_selection is not None:
            data_mode = "include"
        elif intent is not cspec.CopyIntent.structure_only:
            data_mode = "all"

        # --- Charset/collation del destino ----------------------------------------- #
        if "target_charset" in sent and spec.get("target_charset") is not None:
            cs = spec["target_charset"]
            if cs.get("mode") == "override":
                job.target_charset, job.target_collation = self._resolve_charset(
                    job, cs.get("charset"), cs.get("collation")
                )
            else:
                job.target_charset = None
                job.target_collation = None

        # --- Owner (solo PostgreSQL) ----------------------------------------------- #
        if "target_owner_user_id" in sent:
            owner_id = spec.get("target_owner_user_id")
            if owner_id is None:
                job.target_owner_user_id = None
                job.target_owner = None
            else:
                owner = session.get(ServerUser, owner_id)
                if owner is None or owner.server_id != job.target_server_id:
                    raise AppHttpException(
                        message="target_owner_user_id debe ser un usuario del servidor destino.",
                        status_code=422,
                        public_context={"code": cspec.CODE_OWNER_INVALID},
                        context={"target_owner_user_id": owner_id},
                    )
                job.target_owner_user_id = owner.id
                job.target_owner = owner.username

        # ``data_on_existing`` solo tiene sentido en 'data_only': en los otros modos las
        # tablas las crea este mismo job y nacen vacías. Se LIMPIA en vez de arrastrarlo a
        # ``validate_spec``, que lo rechazaría con un 422 del que no se puede salir. Cubre
        # dos caminos reales: un plan creado con el atajo legacy que ya traiga el valor, y un
        # operador que cambia la intención de 'data_only' a otra en un segundo preview.
        if intent is not cspec.CopyIntent.data_only:
            job.data_on_existing = None

        # --- Coherencia del spec completo ------------------------------------------ #
        violations = cspec.validate_spec(
            intent=intent,
            data_mode=data_mode,
            on_existing=(
                cspec.DataOnExisting(job.data_on_existing) if job.data_on_existing else None
            ),
            target_mode=job.target_mode,
            clean_mode=job.clean_mode,
            adopt_target=job.adopt_target,
            charset_override=bool(job.target_charset or job.target_collation),
            owner_requested=job.target_owner is not None,
            target_engine=job.target_engine,
        )
        if violations:
            first = violations[0]
            raise AppHttpException(
                message=first.message,
                status_code=422,
                public_context={
                    "code": first.code,
                    "violations": [
                        {"code": v.code, "message": v.message, **v.detail} for v in violations
                    ],
                },
                context={"violations": [v.code for v in violations]},
            )

        # --- datos ⊆ estructura ---------------------------------------------------- #
        self._check_data_subset(job, intent)
        job.include_data = intent is not cspec.CopyIntent.structure_only

    @staticmethod
    def _reject_unknown_names(resolved, mode: str, which: str) -> None:
        """
        Un nombre pedido EXPLÍCITAMENTE que el catálogo no tiene es un 422, no un silencio.

        Sin esto, "copiá los datos de pedidos_2024" mal tecleado resuelve a la lista vacía y
        el job termina ``succeeded`` con 0 filas copiadas. En ``all_except`` es solo un aviso
        (excluir algo que ya no existe es un spec viejo, no un error), mismo criterio que el
        export.
        """
        if resolved.unknown_names and mode == "include":
            raise AppHttpException(
                message=(
                    f"Estos nombres no existen en el origen: "
                    f"{', '.join(resolved.unknown_names)}."
                ),
                status_code=422,
                public_context={
                    "code": cspec.CODE_UNKNOWN_NAMES,
                    "unknown_names": list(resolved.unknown_names),
                    "selection": which,
                },
                context={},
            )

    def _check_data_subset(self, job: CloneJob, intent) -> None:
        """
        ``datos ⊆ estructura``, con la excepción ya razonada del export: si el DDL del
        contenedor y el de las entidades son ambos ``NONE``, la copia es "solo datos" y la
        restricción no aplica. Se reusa ``export_spec.check_data_subset`` en vez de repetir el
        criterio.

        Se compara contra el CIERRE de la selección de estructura (lo que realmente se va a
        crear), no contra la selección cruda: contra la cruda, una tabla de datos que el
        cierre sí iba a incluir daría un 422 espurio.
        """
        if not job.data_selection:
            return
        data_tables = json.loads(job.data_selection)
        structure_keys: set[str] = set()
        if job.selection:
            structure_keys = {
                r["name"] for r in json.loads(job.selection) if r["object_type"] == "table"
            }
        structure_sel = espec.ResolvedSelection(
            objects=tuple(
                espec.CatalogObject(object_type="table", name=n) for n in structure_keys
            )
        )
        data_sel = espec.ResolvedSelection(
            objects=tuple(
                espec.CatalogObject(object_type="table", name=n) for n in data_tables
            )
        )
        opts = espec.StructureOptions(
            scope_ddl=cspec.scope_ddl_for(job.target_mode, job.clean_mode),
            entity_ddl=cspec.entity_ddl_for(intent),
        )
        # Un clon COMPLETO cubre todas las tablas: no hay selección de estructura que
        # comparar y la restricción se satisface trivialmente.
        if job.is_full_clone and opts.entity_ddl != espec.EntityDdl.NONE:
            return
        missing = espec.check_data_subset(structure_sel, data_sel, opts)
        if missing:
            raise AppHttpException(
                message=(
                    f"Estas tablas piden datos pero no están en la selección de estructura, "
                    f"así que no existirían en el destino: {', '.join(sorted(missing))}."
                ),
                status_code=422,
                public_context={
                    "code": cspec.CODE_DATA_WITHOUT_STRUCTURE,
                    "tables": sorted(missing),
                },
                context={},
            )

    def _resolve_charset(
        self, job: CloneJob, charset: str | None, collation: str | None
    ) -> tuple[str | None, str | None]:
        """
        Valida el par contra el catálogo GLOBAL y devuelve su forma CANÓNICA.

        Dos capas, y la segunda no es opcional: el catálogo es necesario pero **no
        suficiente**. ``engine_family`` mete MySQL y MariaDB en la misma familia y no tienen
        las mismas collations (``utf8mb4_0900_ai_ci`` es solo de MySQL 8, las
        ``utf8mb4_uca1400_*`` solo de MariaDB reciente), y en PostgreSQL la collation es un
        locale del SISTEMA OPERATIVO que puede no existir en ese host. Así que después del
        catálogo se pregunta al MOTOR destino.

        Que esto corra en el ``preview`` —y no en el worker— es lo que evita el peor caso: con
        ``clean_mode='drop_database'`` el pipeline hace DROP y después CREATE, así que un par
        que el motor rechaza dejaría el destino BORRADO y el job fallado.
        """
        dialect = job.target_engine
        try:
            canon_cs, canon_co = charset_catalog.resolve_enabled_combination(
                dialect, charset, collation
            )
        except AppHttpException as exc:
            raise AppHttpException(
                message=exc.message,
                status_code=422,
                public_context={
                    "code": cspec.CODE_CHARSET_COMBINATION_DISABLED,
                    "charset": charset,
                    "collation": collation,
                },
                context={"dialect": dialect},
            ) from exc
        if canon_cs is None and canon_co is None:
            return None, None
        session = self._session()
        try:
            tgt_server = get_server_or_404(session, job.target_server_id)
            tgt_target = build_target(tgt_server)
        finally:
            session.close()
        adapter = get_adapter(tgt_target)
        supported = adapter.supports_charset_combination(canon_cs, canon_co)
        if supported is False:
            raise AppHttpException(
                message=(
                    f"El servidor destino no ofrece la combinación "
                    f"{canon_cs or '(default)'}/{canon_co or '(default)'}. El catálogo la "
                    f"habilita, pero este motor concreto no la tiene."
                ),
                status_code=422,
                public_context={
                    "code": cspec.CODE_CHARSET_UNSUPPORTED_BY_ENGINE,
                    "charset": canon_cs,
                    "collation": canon_co,
                },
                context={"server_id": job.target_server_id},
            )
        return canon_cs, canon_co

    def preview(self, job_id: int, *, spec: dict | None = None, sent: set[str] | None = None) -> dict:
        """
        Resuelve el plan final SIN ejecutar, lo CONGELA y devuelve el ``confirm_token``.

        ``sent`` son los campos PRESENTES en el request; ``spec`` sus valores. La distinción no
        es cosmética: ``selection: null`` significa "clon completo" y su AUSENCIA significa "no
        toques la selección". El parámetro posicional ``selection`` y el flag
        ``update_selection`` que existían antes se eliminaron a propósito: hacían que la
        selección se sobrescribiera en cada llamada, y dejarlos como parámetros muertos sería
        dejar servido el mismo error para el próximo llamador.

        Si hay ``blocking_issues`` el plan se devuelve igual (200) pero **sin token**: el
        operador tiene que poder VER por qué no se puede ejecutar. Un 422 acá dejaría la
        pantalla vacía con un "incompatible" y sin forma de diagnosticar.
        """
        spec = dict(spec or {})
        sent = set(sent or ())

        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            self._guard_still_pending(job)
            if sent:
                self._apply_spec(session, job, spec, sent)
                session.commit()
                session.refresh(job)
            src_server = get_server_or_404(session, job.source_server_id)
            tgt_server = get_server_or_404(session, job.target_server_id)
            src_target = build_target(src_server)
            tgt_target = build_target(tgt_server)
            source_snap, target_snap = self._snapshots_for(job, src_target, tgt_target)
            plan = self._build_execution_plan(
                job, source_snap, target_snap, tgt_target=tgt_target,
                src_target=src_target, with_estimates=True,
            )
            target_engine = job.target_engine
            target_managed_id = job.target_database_id
            clean_mode = job.clean_mode
            target_db_name = job.target_database_name
            effective = {
                "copy_intent": job.copy_intent or CLONE_COPY_STRUCTURE_ONLY,
                # El valor EFECTIVO, no el elegido. La columna guarda solo lo que el operador
                # eligió (NULL en los planes legacy, donde el modo se deriva), pero este campo
                # dice qué va a pasar de verdad con las filas que ya estén en el destino, y
                # eso sale del plan. Devolver NULL acá mientras la fase de datos hace un
                # upsert sería mentir en el campo que el cliente muestra.
                "data_on_existing": (
                    (
                        cspec.DataOnExisting.upsert.value
                        if plan.data_specs[0].upsert
                        else cspec.DataOnExisting.append.value
                    )
                    if plan.data_specs
                    else None
                ),
                "target_charset": job.target_charset,
                "target_collation": job.target_collation,
                "target_owner": job.target_owner,
            }
            # El fingerprint del DESTINO se fija acá: en 'data_only' la validez del plan
            # depende de su esquema tanto como del origen, y hasta ahora nadie lo fijaba.
            job.target_fingerprint = (
                _snapshot_fingerprint(target_snap) if target_snap is not None else None
            )
            token = "" if plan.blocking_issues else self._token_for(job, plan)
            job.confirm_token = token or None
            session.commit()
        finally:
            session.close()

        cross = not _same_family(source_snap.source_engine, target_engine)
        # Para 'drop_database', mostrar una entrada sintética (la op real es a nivel servidor).
        clean_display = [
            {"kind": s.kind, "object_type": s.object_type, "object_name": s.object_name, "sql": s.sql}
            for s in plan.clean_statements
        ]
        if clean_mode == CLONE_CLEAN_DROP_DATABASE:
            clean_display.insert(0, {
                "kind": "clean", "object_type": "database", "object_name": target_db_name,
                "sql": f"DROP DATABASE {target_db_name}; CREATE DATABASE {target_db_name}",
            })
        return {
            "job_id": job_id,
            "target_database_id": target_managed_id,
            "cross_engine": cross,
            "clean_statements": clean_display,
            "structure_statements": [
                {"kind": s.kind, "object_type": s.object_type, "object_name": s.object_name, "sql": s.sql}
                for s in plan.structure_statements
            ],
            "data_tables": [
                {
                    "table": d.table, "row_estimate": d.row_estimate,
                    "row_estimate_known": d.row_estimate_known,
                    "has_primary_key": d.has_primary_key, "upsert": d.upsert,
                }
                for d in plan.data_specs
            ],
            "skipped": plan.skipped,
            "will_adopt": plan.will_adopt,
            "warnings": plan.warnings,
            "notices": plan.notices,
            "blocking_issues": plan.blocking_issues,
            "confirm_token": token,
            **effective,
        }

    @staticmethod
    def _guard_still_pending(job: CloneJob) -> None:
        """
        Un job ya ejecutado no se puede re-previsualizar.

        Si se pudiera, el spec, el fingerprint y el token del job se sobrescribirían y el
        plan dejaría de describir lo que realmente se ejecutó — la traza de auditoría pasaría
        a mentir. Mismo guard (y mismo motivo) que el del export.
        """
        if job.status != CLONE_STATUS_PENDING:
            raise AppHttpException(
                message=(
                    f"El job ya está en estado '{job.status}': no se puede volver a "
                    f"previsualizar. Creá un plan nuevo."
                ),
                status_code=409,
                public_context={"code": cspec.CODE_ALREADY_EXECUTED},
                context={"status": job.status},
            )

    def _assert_not_expired(self, job: CloneJob) -> None:
        if job.expires_at < _utcnow():
            raise AppHttpException(
                message="El plan de clonación expiró; vuelve a crearlo.",
                status_code=410,
                public_context={"code": cspec.CODE_PLAN_EXPIRED},
                context={"clone_job_id": job.id},
            )

    def list_clones(
        self,
        *,
        offset: int,
        limit: int,
        statuses: list[str] | None = None,
        source_server_id: int | None = None,
        target_server_id: int | None = None,
        search: str | None = None,
        batch_id: int | None = None,
        include_batch_children: bool = True,
        order_by: str = "created_at",
        order: str = "desc",
    ) -> tuple[list[dict], int]:
        """
        Historial paginado de clones, del más nuevo al más viejo.

        **Este endpoint faltaba, y su ausencia no era una comodidad de menos: era la razón
        por la que un clon quedaba INALCANZABLE.** El id del job solo existía en la memoria
        del navegador; sin un listado, perderlo era perder el acceso a la operación (la fila
        y sus ítems seguían en la BD, sin ningún camino hacia ellos).

        ``duration_ms`` se calcula en el SERVIDOR y no se deriva en el cliente, porque es lo
        que habilita ordenar por él — «¿cuál fue el más lento?» no se puede contestar
        ordenando la página visible.

        ``batch_id``/``batch_seq`` salen de un LEFT JOIN contra ``clone_batch_items``: la
        relación vive solo de ese lado (``CloneJob`` no sabe que nació de un lote). Sin ese
        dato, los N hijos de un lote son N filas indistinguibles de clones sueltos y entierran
        el historial — de ahí también ``include_batch_children``.
        """
        from app.models.clone_batch import CloneBatchItem

        session = self._session()
        try:
            query = (
                session.query(
                    CloneJob,
                    CloneBatchItem.batch_id.label("batch_id"),
                    CloneBatchItem.seq.label("batch_seq"),
                )
                .outerjoin(CloneBatchItem, CloneBatchItem.clone_job_id == CloneJob.id)
            )

            if statuses:
                query = query.filter(CloneJob.status.in_(statuses))
            if source_server_id is not None:
                query = query.filter(CloneJob.source_server_id == source_server_id)
            if target_server_id is not None:
                query = query.filter(CloneJob.target_server_id == target_server_id)
            if batch_id is not None:
                query = query.filter(CloneBatchItem.batch_id == batch_id)
            elif not include_batch_children:
                query = query.filter(CloneBatchItem.batch_id.is_(None))
            if search:
                # Coincidencia parcial sobre los DOS nombres de base: es un solo campo en la
                # UI porque el operador no sabe (ni le importa) de qué lado estaba el nombre
                # que recuerda.
                pattern = f"%{search.strip()}%"
                query = query.filter(
                    or_(
                        CloneJob.source_database_name.like(pattern),
                        CloneJob.target_database_name.like(pattern),
                    )
                )

            total = query.count()

            if order_by == "duration_ms":
                # El motor no tiene un TIMESTAMPDIFF portable entre MySQL y PostgreSQL, así
                # que se ordena por `finished_at` como proxy y el desempate real lo hace el
                # cliente sobre la página. Los que nunca terminaron van al final.
                columna = CloneJob.finished_at
            else:
                columna = CloneJob.created_at
            columna = columna.desc() if order == "desc" else columna.asc()
            # `id` como segundo criterio: dos jobs creados en el mismo segundo tienen que
            # tener un orden ESTABLE, o la paginación repite y saltea filas entre páginas.
            rows = query.order_by(columna, CloneJob.id.desc()).offset(offset).limit(limit).all()

            out = []
            for job, b_id, b_seq in rows:
                payload = self._serialize_summary(job)
                payload["batch_id"] = b_id
                payload["batch_seq"] = b_seq
                payload["duration_ms"] = (
                    int((job.finished_at - job.started_at).total_seconds() * 1000)
                    if job.started_at and job.finished_at
                    else None
                )
                out.append(payload)
            return out, total
        finally:
            session.close()

    def list_items(self, job_id: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._job_or_404(session, job_id)
            q = session.query(CloneJobItem).filter(CloneJobItem.job_id == job_id)
            total = q.count()
            rows = q.order_by(CloneJobItem.seq.asc()).limit(limit).offset(offset).all()
            return [self._serialize_item(r) for r in rows], total
        finally:
            session.close()

    def cancel(self, job_id: int, *, admin: dict | None = None) -> dict:
        """Solicita la cancelación COOPERATIVA (el worker corta en el próximo punto seguro)."""
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            if job.status not in (CLONE_STATUS_PENDING, CLONE_STATUS_RUNNING):
                raise AppHttpException(
                    message=f"El job no se puede cancelar en estado '{job.status}'.",
                    status_code=409,
                    context={"status": job.status},
                )
            job.cancel_requested = True
            session.commit()
            session.refresh(job)
            return self._serialize_summary(job)
        finally:
            session.close()

    def sweep_interrupted(self) -> int:
        """Marca ``running → interrupted`` (barrido de arranque tras un reinicio)."""
        session = self._session()
        try:
            rows = session.query(CloneJob).filter(CloneJob.status == CLONE_STATUS_RUNNING).all()
            for job in rows:
                job.status = CLONE_STATUS_INTERRUPTED
                job.finished_at = _utcnow()
                job.error = "El proceso se reinició mientras el job estaba en ejecución."
            session.commit()
            return len(rows)
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Execute (valida y encola el job asíncrono)                          #
    # ------------------------------------------------------------------ #
    def execute_clone(
        self, job_id: int, *, confirm_target_name: str, confirm_token: str,
        force: bool = False, admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            self._guard_still_pending(job)
            if confirm_target_name != job.target_database_name:
                raise AppHttpException(
                    message="confirm_target_name no coincide con el nombre de la BD destino.",
                    status_code=422,
                    public_context={"code": cspec.CODE_CONFIRM_NAME_MISMATCH},
                    context={},
                )
            # Cuarentena (solo destino gestionado).
            if job.target_database_id is not None and not force:
                md = session.get(ManagedDatabase, job.target_database_id)
                if md is not None and md.status == ProvisionStatus.error:
                    raise AppHttpException(
                        message="El destino está en cuarentena (status=error). Reintenta con force=true.",
                        status_code=409,
                        public_context={"code": cspec.CODE_TARGET_QUARANTINED},
                        context={"target_database_id": job.target_database_id},
                    )
            src_server = get_server_or_404(session, job.source_server_id)
            tgt_server = get_server_or_404(session, job.target_server_id)
            src_target = build_target(src_server)
            tgt_target = build_target(tgt_server)
            target_ref = f"{job.target_server_id}:{job.target_database_name}"
            clean_mode = job.clean_mode
            target_mode = job.target_mode
            copy_intent = job.copy_intent or CLONE_COPY_STRUCTURE_ONLY
            on_existing = job.data_on_existing
            server_id = job.target_server_id
            managed_id = job.target_database_id
        finally:
            session.close()

        # Anti-TOCTOU: re-snapshotear y revalidar el token contra el plan ACTUAL.
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            source_snap, target_snap = self._snapshots_for(job, src_target, tgt_target)
            if _snapshot_fingerprint(source_snap) != job.source_fingerprint:
                raise AppHttpException(
                    message="El esquema del origen cambió desde que se creó el plan; vuelve a crearlo.",
                    status_code=409,
                    public_context={"code": cspec.CODE_SOURCE_FINGERPRINT_CHANGED},
                    context={"clone_job_id": job_id},
                )
            # Y el del DESTINO, cuando el plan depende de él: en 'data_only' la corrección de
            # la copia se apoya en el esquema del destino, así que alguien que le agregue una
            # columna NOT NULL o una clave única entre el preview y el execute cambia el plan
            # que se confirmó. Sin este chequeo eso pasaba en silencio.
            if (
                job.target_fingerprint is not None
                and target_snap is not None
                and _snapshot_fingerprint(target_snap) != job.target_fingerprint
            ):
                raise AppHttpException(
                    message=(
                        "El esquema del destino cambió desde que se previsualizó el plan; "
                        "vuelve a previsualizar."
                    ),
                    status_code=409,
                    public_context={"code": cspec.CODE_TARGET_FINGERPRINT_CHANGED},
                    context={"clone_job_id": job_id},
                )
            plan = self._build_execution_plan(
                job, source_snap, target_snap, tgt_target=tgt_target
            )
            # El guard de compatibilidad se evalúa de nuevo acá, no solo en el preview: es la
            # última barrera antes de encolar, y el destino pudo cambiar.
            if plan.blocking_issues:
                raise AppHttpException(
                    message=(
                        "El esquema del destino no admite la copia de datos: "
                        + "; ".join(
                            self._issue_message(cspec.CompatIssue(**i))
                            for i in plan.blocking_issues[:5]
                        )
                    ),
                    status_code=422,
                    public_context={
                        "code": cspec.CODE_TARGET_SCHEMA_INCOMPATIBLE,
                        "issues": plan.blocking_issues,
                    },
                    context={},
                )
            expected = self._token_for(job, plan)
            if confirm_token != expected:
                raise AppHttpException(
                    message="confirm_token no coincide con el plan actual; vuelve a previsualizar.",
                    status_code=422,
                    public_context={"code": cspec.CODE_TOKEN_MISMATCH},
                    context={},
                )
        finally:
            session.close()

        # Auditoría de intención fail-closed ANTES de encolar (rastro durable garantizado).
        audit.record_intent(
            "clone.execute",
            admin=admin,
            target_type="managed_database",
            target_id=managed_id,
            server_id=server_id,
            detail=(
                f"clon {job_id} → {target_ref} (copy={copy_intent}, clean={clean_mode}, "
                f"mode={target_mode}, on_existing={on_existing}, "
                f"tablas_con_datos={len(plan.data_specs)})"
            ),
        )

        from app.services import clone_runner
        clone_runner.enqueue(job_id)
        return self.get_plan(job_id)

    # ------------------------------------------------------------------ #
    # Ejecución asíncrona (corre en un worker de clone_runner)            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_error(exc: Exception) -> str:
        orig = getattr(exc, "orig", None)
        return str(orig if orig is not None else exc)[:500]

    def abort_pending_job(self, job_id: int, *, reason: str) -> bool:
        """
        Cierra como ``failed`` un job que quedó en ``pending`` y que NUNCA se va a ejecutar.

        Existe para el LOTE, y por eso es público: cuando el preview de una fila devuelve
        ``blocking_issues``, el job ya está creado pero no puede correr, y dejarlo ``pending``
        para siempre ensucia el barrido de arranque (que solo mira ``running``) y el historial.
        La alternativa era que el orquestador llamara a ``_set_status``, o sea que otro módulo
        dependiera de un detalle interno de éste.

        Solo actúa sobre ``pending``, con ``UPDATE`` condicional: si un worker ya lo reclamó
        —imposible por construcción hoy, pero no algo que este método deba asumir— no le pisa
        el estado. Devuelve si efectivamente lo cerró.
        """
        session = self._session()
        try:
            closed = (
                session.query(CloneJob)
                .filter(CloneJob.id == job_id, CloneJob.status == CLONE_STATUS_PENDING)
                .update(
                    {
                        CloneJob.status: CLONE_STATUS_FAILED,
                        CloneJob.error: reason,
                        CloneJob.finished_at: _utcnow(),
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            return bool(closed)
        finally:
            session.close()

    def request_cancel(self, job_id: int) -> bool:
        """
        Pide la cancelación COOPERATIVA de un job sin pasar por la ruta HTTP.

        La usa el lote: cancelar el lote mientras una fila está copiando tiene que detener
        TAMBIÉN esa copia, y el chequeo entre filas del orquestador no alcanza — el worker de
        la fila puede estar horas dentro de una tabla grande. Sin esto, "cancelar" dejaba
        correr hasta el final la base en curso.

        No valida el estado ni audita: eso es responsabilidad de ``cancel_clone`` (la ruta),
        que además tiene el admin de la request. Devuelve si marcó algo.
        """
        session = self._session()
        try:
            marked = (
                session.query(CloneJob)
                .filter(
                    CloneJob.id == job_id,
                    CloneJob.status.in_([CLONE_STATUS_PENDING, CLONE_STATUS_RUNNING]),
                )
                .update({CloneJob.cancel_requested: True}, synchronize_session=False)
            )
            session.commit()
            return bool(marked)
        finally:
            session.close()

    def _set_status(self, job_id, status, *, phase=None, error=None, finished=False):
        session = self._session()
        try:
            job = session.get(CloneJob, job_id)
            if job is None:
                return
            job.status = status
            if phase is not None:
                job.phase = phase
            if error is not None:
                job.error = error
            if finished:
                job.finished_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def _set_progress(self, job_id, progress: dict):
        session = self._session()
        try:
            job = session.get(CloneJob, job_id)
            if job is not None:
                job.progress = json.dumps(progress)
                session.commit()
        finally:
            session.close()

    def _record_items(self, job_id, rows: list[dict]):
        if not rows:
            return
        session = self._session()
        try:
            for r in rows:
                session.add(CloneJobItem(job_id=job_id, **r))
            session.commit()
        finally:
            session.close()

    def _cancel_checker(self, job_id):
        """Callable que lee ``cancel_requested`` de la BD, cacheado 2s para no martillar."""
        state = {"val": False, "ts": 0.0}

        def check() -> bool:
            now = time.monotonic()
            if now - state["ts"] > 2.0:
                session = self._session()
                try:
                    job = session.get(CloneJob, job_id)
                    state["val"] = bool(job.cancel_requested) if job else False
                finally:
                    session.close()
                state["ts"] = now
            return state["val"]

        return check

    def run_job(self, job_id: int) -> None:
        """Pipeline completo del clon (limpiar → estructura → datos → adopt). Best-effort
        con reporte por ítem; nunca lanza (registra el fallo en el job)."""
        # 1) Reclamar ATÓMICAMENTE (pending -> running): UPDATE condicional + rowcount.
        #    Si dos workers compiten por el mismo job, solo uno afecta 1 fila; el otro sale.
        session = self._session()
        try:
            claimed = (
                session.query(CloneJob)
                .filter(CloneJob.id == job_id, CloneJob.status == CLONE_STATUS_PENDING)
                .update(
                    {CloneJob.status: CLONE_STATUS_RUNNING, CloneJob.started_at: _utcnow(),
                     CloneJob.error: None},
                    synchronize_session=False,
                )
            )
            session.commit()
            if not claimed:
                return  # otro worker ya lo tomó (o no está pending)
        finally:
            session.close()

        from app.services import clone_runner

        # 2) Cargar contexto (targets, campos del job).
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            src_server = get_server_or_404(session, job.source_server_id)
            tgt_server = get_server_or_404(session, job.target_server_id)
            src_target = build_target(src_server)
            tgt_target = build_target(tgt_server)
            ctx = {
                "source_db": job.source_database_name,
                "target_db": job.target_database_name,
                "target_engine": job.target_engine,
                "target_ref": f"{job.target_server_id}:{job.target_database_name}",
                "managed_id": job.target_database_id,
                "server_id": job.target_server_id,
                "clean_mode": job.clean_mode,
                "target_mode": job.target_mode,
                "copy_intent": job.copy_intent or CLONE_COPY_STRUCTURE_ONLY,
                "on_existing": job.data_on_existing,
                "target_charset": job.target_charset,
                "target_collation": job.target_collation,
                "target_owner": job.target_owner,
                "batch_rows": CLONE_DATA_BATCH_ROWS,
                "source_fp": job.source_fingerprint,
                "src_managed_id": job.source_database_id,
                "adopt_owner_id": job.adopt_owner_id,
            }
        finally:
            session.close()

        guard = clone_runner.target_guard(ctx["target_ref"])
        with guard:
            try:
                self._pipeline(job_id, src_target, tgt_target, ctx)
            except AppHttpException as exc:
                self._set_status(job_id, CLONE_STATUS_FAILED, error=exc.message, finished=True)
            except Exception as exc:  # noqa: BLE001
                logger.error("Pipeline de clon %s falló", job_id, exc_info=True)
                self._set_status(job_id, CLONE_STATUS_FAILED, error=self._clean_error(exc), finished=True)

    def _pipeline(self, job_id, src_target, tgt_target, ctx):
        cancel = self._cancel_checker(job_id)
        src_adapter = get_adapter(src_target)
        tgt_adapter = get_adapter(tgt_target)
        engine = EngineType(ctx["target_engine"])
        lock_key = ctx["managed_id"] if ctx["managed_id"] is not None else _synthetic_lock_key(
            ctx["server_id"], ctx["target_db"]
        )
        runner = MigrationRunner()

        # Todas las fases MUTANTES (limpiar → estructura → datos → adopt) corren bajo UN
        # ÚNICO advisory lock del motor, sostenido durante todo el pipeline en una conexión
        # dedicada del worker. Así se serializan cross-proceso dos clones al mismo destino
        # (o un clon vs. un execute de schema-comparison sobre la misma BD física) — el
        # lock abarca también DROP/CREATE DATABASE y la fase de datos, no solo el DDL.
        #
        # Los SNAPSHOTS y el armado del plan van DENTRO del lock, no antes. El worker puede
        # esperar el lock varios minutos (lo puede tener otro clon o un ``apply`` de
        # migración sobre la misma BD), y en 'data_only' la corrección de la copia depende del
        # esquema del destino: snapshotear afuera dejaba una ventana en la que el guard de
        # compatibilidad validaba contra un esquema ya viejo.
        with runner.advisory_lock(tgt_target, engine=engine, lock_key=lock_key):
            # Anti-TOCTOU final (el origen pudo cambiar entre execute y run).
            source_snap = src_adapter.structural_snapshot(ctx["source_db"])
            if _snapshot_fingerprint(source_snap) != ctx["source_fp"]:
                self._set_status(job_id, CLONE_STATUS_FAILED, finished=True,
                                 error="El esquema del origen cambió antes de ejecutar; replanea.")
                return

            session = self._session()
            try:
                job = self._job_or_404(session, job_id)
                target_snap = None
                if self._needs_target_snapshot(job):
                    target_snap = tgt_adapter.structural_snapshot(ctx["target_db"])
                # Reconstruir el plan en el worker (fuente de verdad final).
                plan = self._build_execution_plan(
                    job, source_snap, target_snap, tgt_target=tgt_target
                )
            finally:
                session.close()

            if plan.blocking_issues:
                # El destino cambió entre el execute y el arranque del worker: no se toca el
                # motor. El detalle va con vocabulario cerrado, nunca el mensaje del motor.
                self._set_status(
                    job_id, CLONE_STATUS_FAILED, finished=True,
                    error=(
                        "El esquema del destino no admite la copia de datos: "
                        + "; ".join(
                            self._issue_message(cspec.CompatIssue(**i))
                            for i in plan.blocking_issues[:5]
                        )
                    ),
                )
                return

            self._run_phases(
                job_id, runner, src_target, tgt_target, ctx, plan, source_snap, cancel, engine, lock_key
            )

    def _run_phases(self, job_id, runner, src_target, tgt_target, ctx, plan, source_snap, cancel, engine, lock_key):
        """Fases mutantes del clon, ejecutadas DENTRO del advisory lock del pipeline."""
        tgt_adapter = get_adapter(tgt_target)
        seq = 0
        had_failure = False
        progress: dict = {"phase": None, "tables": {}}

        # --- Fase: preparar BD destino (crear/limpiar) ------------------------------- #
        self._set_status(job_id, CLONE_STATUS_RUNNING, phase="clean")
        progress["phase"] = "clean"
        if cancel():
            self._set_status(job_id, CLONE_STATUS_CANCELED, finished=True)
            return
        # Charset/collation de la BD destino, en orden de precedencia:
        #
        # 1. Lo que el operador ELIGIÓ en el preview, ya en su forma canónica del catálogo y
        #    ya verificado contra el motor destino (``_resolve_charset``). Nunca llega texto
        #    crudo del request hasta acá.
        # 2. Si no eligió: el MISMO default que la origen, para que no derive (era la causa
        #    raíz del loop de falsos positivos del diff: un default de BD distinto hacía que
        #    la MISMA collation física se juzgara "explícita" en un lado y "heredada" en el
        #    otro). Solo mismo motor: los nombres de collation/locale no son portables
        #    cross-engine → ahí se cae al default del motor destino.
        same_engine = source_snap.source_engine == tgt_adapter.dialect
        db_charset = ctx.get("target_charset") or (
            source_snap.db_charset if same_engine else None
        )
        db_collation = ctx.get("target_collation") or (
            source_snap.db_collation if same_engine else None
        )
        # ``owner`` solo tiene semántica en PostgreSQL; el adapter de MySQL lo ignora.
        db_owner = ctx.get("target_owner")
        if ctx["target_mode"] == CLONE_TARGET_NEW:
            tgt_adapter.create_database(
                ctx["target_db"], charset=db_charset, collation=db_collation, owner=db_owner
            )
            self._record_items(job_id, [dict(seq=seq, kind=CLONE_ITEM_CLEAN, object_type="database",
                                             object_name=ctx["target_db"], status=CLONE_ITEM_APPLIED,
                                             executed_at=_utcnow())])
            seq += 1
        elif ctx["clean_mode"] == CLONE_CLEAN_DROP_DATABASE:
            tgt_adapter.drop_database(ctx["target_db"])
            tgt_adapter.create_database(
                ctx["target_db"], charset=db_charset, collation=db_collation, owner=db_owner
            )
            self._record_items(job_id, [dict(seq=seq, kind=CLONE_ITEM_CLEAN, object_type="database",
                                             object_name=ctx["target_db"], status=CLONE_ITEM_APPLIED,
                                             executed_at=_utcnow())])
            seq += 1
        elif ctx["clean_mode"] == CLONE_CLEAN_OBJECTS and plan.clean_statements:
            # disable_fk_checks: el orden topológico inverso de los DROP (schema_diff.py)
            # cubre el caso normal, pero no puede ver una FK desde OTRA base de datos del
            # mismo servidor (fuera del snapshot) ni un ciclo de FKs — defensa en profundidad.
            seq, failed = self._run_statements(
                job_id, runner, tgt_target, ctx["target_db"], engine, lock_key,
                plan.clean_statements, CLONE_ITEM_CLEAN, seq, disable_fk_checks=True,
            )
            had_failure = had_failure or failed

        # Triggers/eventos se difieren a DESPUÉS de la fase de datos (ver el bloque de más
        # abajo): si el origen tiene un trigger de INSERT que puebla otra tabla, crearlo ANTES
        # de copiar los datos lo dispararía durante la copia y duplicaría filas en la tabla
        # que puebla (ER_DUP_ENTRY 1062 en una pivote poblada por trigger, p. ej.).
        body_post = [s for s in plan.structure_statements if s.object_type in _POST_DATA_BODY_TYPES]

        # --- Fase: estructura -------------------------------------------------------- #
        if not had_failure and plan.structure_statements:
            self._set_status(job_id, CLONE_STATUS_RUNNING, phase="structure")
            if cancel():
                self._set_status(job_id, CLONE_STATUS_CANCELED, finished=True)
                return
            # Objetos "duros" (tablas/columnas/FKs/índices): orden determinista ya correcto
            # (topológico + FKs en fase aditiva) → una pasada, corta al primer fallo.
            # Objetos con CUERPO SIN efectos en INSERT (vistas/rutinas): pueden depender entre
            # sí en cualquier orden → reintento diferido. Triggers/eventos NO van acá (body_post).
            hard = [s for s in plan.structure_statements if s.object_type not in _BODY_TYPES]
            body_pre = [
                s for s in plan.structure_statements
                if s.object_type in _BODY_TYPES and s.object_type not in _POST_DATA_BODY_TYPES
            ]
            if hard:
                seq, failed = self._run_statements(
                    job_id, runner, tgt_target, ctx["target_db"], engine, lock_key,
                    hard, CLONE_ITEM_STRUCTURE, seq,
                )
                had_failure = had_failure or failed
            if not had_failure and body_pre:
                seq, failed = self._run_body_statements(
                    job_id, runner, tgt_target, ctx["target_db"], engine, lock_key, body_pre, seq,
                )
                had_failure = had_failure or failed

        # --- Fase: datos ------------------------------------------------------------- #
        # UNA sola fuente de verdad para "¿hay datos?": el plan. Antes esto dependía además
        # de ``ctx["include_data"]``, leído de una columna aparte: si las dos fuentes
        # discrepaban (p. ej. un job creado antes de un deploy), el worker copiaba todo o
        # nada según cuál ganara.
        if not had_failure and plan.data_specs:
            self._set_status(job_id, CLONE_STATUS_RUNNING, phase="data")
            progress["phase"] = "data"

            # Throttle temporal: persistir el progreso a la BD del gateway a lo sumo cada
            # ~3s. Sin esto, una tabla grande dispara un UPDATE por lote (p. ej. 50M filas /
            # 1000 = 50k updates), martillando la BD de metadatos. El estado final siempre se
            # persiste al cierre del pipeline (_set_progress más abajo).
            _last_persist = [0.0]

            def progress_cb(table, rows_so_far, _p=progress, _jid=job_id, _lp=_last_persist):
                _p["tables"][table] = rows_so_far
                now = time.monotonic()
                if now - _lp[0] >= _CLONE_PROGRESS_PERSIST_SECONDS:
                    _lp[0] = now
                    self._set_progress(_jid, _p)

            specs = [
                TableCopySpec(
                    table=d.table, columns=d.columns, primary_key=d.primary_key,
                    upsert=d.upsert, has_unique_key=d.has_unique_key,
                )
                for d in plan.data_specs
            ]
            results = copy_tables(
                source_target=src_target, source_db=ctx["source_db"], source_engine=source_snap.source_engine,
                dest_target=tgt_target, dest_db=ctx["target_db"], dest_engine=ctx["target_engine"],
                specs=specs, batch_rows=ctx["batch_rows"], progress_cb=progress_cb, cancel_cb=cancel,
            )
            item_rows = []
            for res in results:
                status = CLONE_ITEM_APPLIED if res.status == "applied" else (
                    CLONE_ITEM_SKIPPED if res.status in ("skipped", "canceled") else CLONE_ITEM_FAILED
                )
                # NO persistir el error crudo del driver en pasos de DATOS: puede incluir
                # VALORES de filas (p. ej. "Duplicate entry 'alice@x.com'…") que se filtrarían
                # a la BD de metadatos y a la API. Guardamos un motivo genérico; el detalle
                # completo queda solo en los logs del gateway (data_copy ya lo registra).
                error = None
                if res.status == "failed":
                    had_failure = True
                    error = "Fallo al copiar datos de la tabla (ver logs del gateway)."
                    logger.warning("Clon %s: fallo de datos en tabla %s: %s",
                                   job_id, res.table, res.error)
                # ``execution_ms`` en los pasos de DATOS: la columna existía en el modelo y en
                # el contrato desde siempre, pero las fases de limpieza y estructura eran las
                # únicas que la llenaban. El reporte podía decir cuánto tardó cada CREATE TABLE
                # y no cuánto tardó copiar una tabla — que es la pregunta que se hace.
                item_rows.append(dict(seq=seq, kind=CLONE_ITEM_DATA, object_type="table",
                                      object_name=res.table, status=status, error=error,
                                      rows_copied=res.rows_copied,
                                      execution_ms=res.duration_ms, executed_at=_utcnow()))
                seq += 1
            self._record_items(job_id, item_rows)
            if any(r.status == "canceled" for r in results):
                self._set_status(job_id, CLONE_STATUS_CANCELED, phase="data", finished=True)
                return
            if ctx["target_engine"] == "postgresql":
                self._resync_postgres_identity_sequences(
                    tgt_target, ctx["target_db"], source_snap, results,
                )

        # --- Fase: triggers y eventos (DESPUÉS de los datos) -------------------------- #
        # Diferidos a propósito: crearlos recién ahora, con los datos ya cargados, evita que
        # un trigger de INSERT del origen se dispare durante la copia y duplique/altere filas
        # de la tabla que puebla (síntoma real: ER_DUP_ENTRY 1062 en una tabla pivote como
        # users_modules_permissions). MySQL/MariaDB NO desactivan triggers con
        # FOREIGN_KEY_CHECKS=0, así que la única defensa portable es el orden (igual que
        # mysqldump, que recrea los triggers al final, tras cargar los datos). Reintento
        # diferido para dependencias entre ellos; las tablas/rutinas de las que dependen ya
        # existen (fase de estructura). Corre aunque include_data=False (sin datos que copiar,
        # simplemente se crean acá en vez de en la fase de estructura).
        if not had_failure and body_post:
            self._set_status(job_id, CLONE_STATUS_RUNNING, phase="structure")
            if cancel():
                self._set_status(job_id, CLONE_STATUS_CANCELED, finished=True)
                return
            seq, failed = self._run_body_statements(
                job_id, runner, tgt_target, ctx["target_db"], engine, lock_key, body_post, seq,
            )
            had_failure = had_failure or failed

        # --- Fase: adopt ------------------------------------------------------------- #
        if not had_failure and plan.will_adopt:
            self._set_status(job_id, CLONE_STATUS_RUNNING, phase="adopt")
            try:
                self._adopt_target(job_id, ctx)
                self._record_items(job_id, [dict(seq=seq, kind=CLONE_ITEM_ADOPT, object_type="database",
                                                 object_name=ctx["target_db"], status=CLONE_ITEM_APPLIED,
                                                 executed_at=_utcnow())])
                seq += 1
            except Exception as exc:  # noqa: BLE001 — adopt no debe tumbar un clon ya aplicado
                logger.warning("Auto-adopt del clon %s falló", job_id, exc_info=True)
                self._record_items(job_id, [dict(seq=seq, kind=CLONE_ITEM_ADOPT, object_type="database",
                                                 object_name=ctx["target_db"], status=CLONE_ITEM_FAILED,
                                                 error=self._clean_error(exc), executed_at=_utcnow())])
                seq += 1

        # --- Cierre ------------------------------------------------------------------ #
        final = CLONE_STATUS_FAILED if had_failure else CLONE_STATUS_SUCCEEDED
        self._set_progress(job_id, progress)
        self._set_status(job_id, final, phase="done", finished=True,
                         error="Al menos un paso falló; ver los ítems." if had_failure else None)
        # Cuarentena del destino gestionado ante fallo (consistente con el flujo apply):
        # protege frente al próximo execute hasta que un admin lo revise (force).
        if had_failure and ctx["managed_id"] is not None:
            self._quarantine_target(ctx["managed_id"])
        # Auditoría de resultado (append-only). El worker corre fuera del ciclo de request:
        # sin Request ID/admin; la intención ya quedó registrada con record_intent al encolar.
        audit.record(
            "clone.execute",
            status="error" if had_failure else "success",
            target_type="managed_database",
            target_id=ctx["managed_id"],
            server_id=ctx["server_id"],
            touched_engine=True,
            detail=(
                f"clon {job_id} → {ctx['target_ref']} (copy={ctx['copy_intent']}, "
                f"clean={ctx['clean_mode']}, mode={ctx['target_mode']}, "
                f"on_existing={ctx['on_existing']}, tablas_con_datos={len(plan.data_specs)}, "
                f"charset={ctx.get('target_charset') or '-'}/"
                f"{ctx.get('target_collation') or '-'})"
            ),
        )

    def _quarantine_target(self, managed_id: int) -> None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, managed_id)
            if md is not None:
                md.status = ProvisionStatus.error
                session.commit()
        finally:
            session.close()

    def _run_statements(self, job_id, runner, tgt_target, db_name, engine, lock_key,
                        statements: list, kind: str, seq: int,
                        *, disable_fk_checks: bool = False) -> tuple[int, bool]:
        """Ejecuta una lista de _StructStmt vía execute_adhoc y registra el resultado por ítem.
        ``already_locked=True``: el pipeline ya sostiene el advisory lock (no re-adquirir)."""
        sqls = [s.sql for s in statements]
        # ``bulk=True``: estas fases emiten DDL que puede tardar MINUTOS sobre un destino
        # con datos —un DROP TABLE de una tabla enorme en clean_mode='objects', un CREATE
        # INDEX— y el default es el timeout INTERACTIVO de 15 s. En MySQL ese valor es un
        # read_timeout de SOCKET del cliente: al expirar, la conexión se rompe MIENTRAS EL
        # MOTOR SIGUE TRABAJANDO, el gateway registra un fallo de una sentencia que en
        # realidad se va a completar, y con stop_on_error=True el clon entero muere. El clon
        # ya es asíncrono y sostiene el advisory lock, así que la espera larga es correcta.
        results = runner.execute_adhoc(
            tgt_target, db_name=db_name, engine=engine, lock_key=lock_key, statements=sqls,
            already_locked=True, disable_fk_checks=disable_fk_checks, bulk=True,
        )
        by_index = {r.index: r for r in results}
        rows = []
        failed = False
        for i, st in enumerate(statements):
            r = by_index.get(i)
            if r is None:
                status, error, ms = CLONE_ITEM_SKIPPED, None, None
            elif r.status == "applied":
                status, error, ms = CLONE_ITEM_APPLIED, None, r.execution_ms
            else:
                status, error, ms = CLONE_ITEM_FAILED, r.error, r.execution_ms
                failed = True
            rows.append(dict(seq=seq, kind=kind, object_type=st.object_type, object_name=st.object_name,
                             sql=st.sql, status=status, error=error, execution_ms=ms,
                             executed_at=_utcnow() if r is not None else None))
            seq += 1
        self._record_items(job_id, rows)
        return seq, failed

    def _run_body_statements(self, job_id, runner, tgt_target, db_name, engine, lock_key,
                             statements: list, seq: int) -> tuple[int, bool]:
        """Ejecuta objetos con cuerpo (vistas/rutinas/triggers/eventos) con REINTENTO
        DIFERIDO: los que fallan por una dependencia aún no creada se reintentan en la
        pasada siguiente. Un objeto solo se marca fallido cuando una pasada COMPLETA no
        crea NINGUNO de los pendientes (sin progreso = fallo real, no de orden). Esto
        resuelve dependencias vista→vista / rutina→vista en cualquier orden sin parsear
        los cuerpos. Cada sentencia es autónoma (AUTOCOMMIT): un fallo no deja estado
        parcial, y los ya aplicados NO se reintentan (no hay 'already exists')."""
        # (índice original, sentencia) — para registrar los ítems en su orden original.
        results: dict[int, tuple[str, str | None, int | None]] = {}
        remaining = list(enumerate(statements))
        while remaining:
            res = runner.execute_adhoc(
                tgt_target, db_name=db_name, engine=engine, lock_key=lock_key,
                statements=[st.sql for _, st in remaining],
                already_locked=True, stop_on_error=False, bulk=True,
            )
            by_pos = {r.index: r for r in res}
            still: list = []
            progressed = False
            for pos, (orig_i, st) in enumerate(remaining):
                r = by_pos.get(pos)
                if r is not None and r.status == "applied":
                    results[orig_i] = ("applied", None, r.execution_ms)
                    progressed = True
                else:
                    results[orig_i] = ("failed", r.error if r else None,
                                       r.execution_ms if r else None)
                    still.append((orig_i, st))
            remaining = still
            if not progressed:
                break  # ninguna dependencia se resolvió: lo que queda es fallo real

        rows = []
        failed = False
        for orig_i, st in enumerate(statements):
            status_key, error, ms = results[orig_i]
            status = CLONE_ITEM_APPLIED if status_key == "applied" else CLONE_ITEM_FAILED
            if status_key == "failed":
                failed = True
            rows.append(dict(seq=seq, kind=CLONE_ITEM_STRUCTURE, object_type=st.object_type,
                             object_name=st.object_name, sql=st.sql, status=status,
                             error=error, execution_ms=ms, executed_at=_utcnow()))
            seq += 1
        self._record_items(job_id, rows)
        return seq, failed

    def _adopt_target(self, job_id, ctx) -> None:
        """Adopta el destino y le stampa el blueprint+versión del origen (clon completo)."""
        from app.controllers.managed_database_controller import (
            ManagedDatabaseController,
        )

        session = self._session()
        try:
            src_md = session.get(ManagedDatabase, ctx["src_managed_id"]) if ctx["src_managed_id"] else None
            if src_md is None or src_md.model_id is None:
                return  # el origen ya no es gestionado con blueprint; nada que adoptar
            model_id = src_md.model_id
            model_version = src_md.model_version
            # El ENTORNO del origen viaja igual que el blueprint y la versión. Sin esto el
            # destino nacía con el entorno por DEFAULT, que es el más permisivo: un clon
            # completo de una base productiva —o sea, una base con la estructura Y LOS DATOS de
            # producción— quedaba clasificada como desarrollo y por lo tanto sin el guard de
            # migraciones destructivas. Heredar es lo correcto acá justamente porque este
            # camino solo corre en el clon COMPLETO: el destino es una réplica del origen.
            environment_id = src_md.environment_id
            existing_tgt = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.server_id == ctx["server_id"],
                        ManagedDatabase.name == ctx["target_db"])
                .one_or_none()
            )
            already_adopted = existing_tgt is not None
        finally:
            session.close()

        if already_adopted:
            return  # ya está en el inventario; no re-adoptar (idempotente)

        ManagedDatabaseController().adopt_database(
            {
                "server_id": ctx["server_id"],
                "name": ctx["target_db"],
                "owner_id": ctx["adopt_owner_id"],
                "model_id": model_id,
                "model_version": model_version,
                "environment_id": environment_id,
            },
            # ``admin=None``: ``clone_jobs`` no persiste quién pidió el job, así que acá no hay
            # autor que propagar. La adopción queda auditada sin autor. Arreglarlo requiere una
            # columna nueva en la tabla; está anotado como ítem propio en TODO.md.
            admin=None,
        )
