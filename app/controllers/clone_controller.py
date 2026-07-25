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
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
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
from app.services import audit
from app.services.db_admin import clone_dependencies as cdeps
from app.services.db_admin.data_copy import TableCopySpec, copy_tables
from app.services.db_admin.dtos import SchemaSnapshot
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import quote_identifier, validate_identifier
from app.services.db_admin.migrations import MigrationRunner
from app.services.db_admin.schema_diff import diff_snapshots

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


@dataclass(frozen=True)
class _ExecutionPlan:
    clean_statements: list[_StructStmt]
    structure_statements: list[_StructStmt]
    data_specs: list[_DataSpec]
    skipped: list[dict]
    will_adopt: bool
    table_order: list[str]
    warnings: list[str]

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
# Tipos con cuerpo procedural: no portables cross-engine (atados al motor de origen).
_PROCEDURAL_TYPES = frozenset({"routine", "trigger", "event"})
# Tipos específicos de un motor sin equivalente directo cross-family.
_ENGINE_SPECIFIC_TYPES = frozenset(
    {"sequence", "enum_type", "extension", "materialized_view"}
)
# Tipos cuyo DDL lleva un CUERPO que puede referenciar OTROS objetos por nombre
# (vistas/rutinas/triggers/eventos). Requieren: (1) re-calificar el esquema origen→destino
# y (2) ejecución con reintento diferido, porque pueden depender entre sí en cualquier orden.
_BODY_TYPES = frozenset({"view", "materialized_view", "routine", "trigger", "event"})
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
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

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

    def _build_inventory(self, snap: SchemaSnapshot, tgt_engine: str, *, include_data: bool) -> dict:
        """Inventario de objetos + portabilidad + grafo de dependencias."""
        row_est: dict[str, int] = {}
        objects = []
        for otype, name in self._iter_objects(snap):
            portable, reason = self._portability(otype, snap.source_engine, tgt_engine)
            objects.append({
                "object_type": otype, "name": name,
                "portable": portable, "portability_reason": reason,
                "row_estimate": row_est.get(name) if (include_data and otype == "table") else None,
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

        # Guarda: origen y destino no pueden ser la MISMA BD física.
        if src.server_id == tgt.server_id and src.database_name == tgt.database_name:
            raise AppHttpException(
                message="El origen y el destino no pueden ser la misma base de datos.",
                status_code=422,
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )

        # Existencia en vivo del origen y del destino.
        src_adapter = get_adapter(src.target)
        tgt_adapter = get_adapter(tgt.target)
        live_source = src_adapter.list_databases()
        if src.database_name not in live_source:
            raise AppHttpException(
                message=f"La BD origen '{src.database_name}' no existe en el servidor.",
                status_code=404,
                context={"server_id": src.server_id, "database": src.database_name},
            )
        target_exists = tgt.database_name in tgt_adapter.list_databases()
        if target_mode == CLONE_TARGET_NEW and target_exists:
            raise AppHttpException(
                message=f"La BD destino '{tgt.database_name}' ya existe. Usá target_mode='existing'.",
                status_code=422,
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )
        if target_mode == CLONE_TARGET_EXISTING and not target_exists:
            raise AppHttpException(
                message=f"La BD destino '{tgt.database_name}' no existe. Usá target_mode='new'.",
                status_code=404,
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )
        if clean_mode != CLONE_CLEAN_NONE and target_mode == CLONE_TARGET_NEW:
            raise AppHttpException(
                message="clean_mode solo aplica a un destino existente (target_mode='existing').",
                status_code=422,
                context={"clean_mode": clean_mode, "target_mode": target_mode},
            )

        # Guarda de auto-adopt: solo clon COMPLETO desde un origen gestionado con blueprint.
        if adopt_target:
            if selection is not None:
                raise AppHttpException(
                    message="adopt_target solo es válido en un clon COMPLETO (sin selección parcial).",
                    status_code=422,
                    context={},
                )
            if src.model_id is None:
                raise AppHttpException(
                    message="adopt_target requiere que el origen sea una BD gestionada con blueprint.",
                    status_code=422,
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
                        context={"adopt_owner_id": owner_id, "target_server_id": tgt.server_id},
                    )
            finally:
                vsession.close()

        # Snapshot del origen (solo lectura) + fingerprint anti-TOCTOU.
        source_snap = src_adapter.structural_snapshot(src.database_name)
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
                f"{tgt.server_id}/{tgt.database_name} (data={include_data}, mode={target_mode})"
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

    def list_objects(self, job_id: int) -> dict:
        """Inventario en vivo del origen + portabilidad + grafo de dependencias."""
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            include_data = job.include_data
            src_target, _ = self._load_side_targets(job)
            tgt_engine = job.target_engine
        finally:
            session.close()
        snap = get_adapter(src_target).structural_snapshot(job.source_database_name)
        return self._build_inventory(snap, tgt_engine, include_data=include_data)

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
    ) -> _ExecutionPlan:
        """
        Arma el plan determinista: sentencias de limpieza (si aplica), sentencias de
        estructura (CREATE en el dialecto destino) y specs de datos. Reutiliza el pipeline
        diff+render: estructura = diff(origen_filtrado vs vacío) → 'new'; limpieza objeto
        por objeto = diff(vacío vs destino) → 'dropped'.
        """
        selection = json.loads(job.selection) if job.selection else None
        tgt_engine = job.target_engine
        tgt_adapter = get_adapter(tgt_target)

        keys = self._closure_keys(source_snap, selection)
        filtered = self._filter_snapshot(source_snap, keys)

        # --- Estructura: diff(origen filtrado vs destino vacío) → todo 'new' ---------- #
        empty_tgt = self._empty_snapshot(tgt_engine, job.target_database_name)
        struct_diff = diff_snapshots(filtered, empty_tgt)
        rendered = tgt_adapter.render_diff(struct_diff)

        structure: list[_StructStmt] = []
        skipped: list[dict] = []
        skipped_names: set[str] = set()
        for r in rendered:
            portable, reason = self._portability(r.object_type, source_snap.source_engine, tgt_engine)
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
        if job.include_data:
            # upsert si preservamos un destino existente; INSERT plano si está limpio/nuevo.
            upsert = job.clean_mode == CLONE_CLEAN_NONE and job.target_mode == CLONE_TARGET_EXISTING
            table_keys = {name for (ot, name) in (keys or set()) if ot == "table"} if keys else None
            ordered = self._data_table_order(filtered)
            for t in ordered:
                if table_keys is not None and t.table not in table_keys:
                    continue
                # Datos solo si la tabla es portable (misma familia siempre; cross-family: sí).
                portable, _ = self._portability("table", source_snap.source_engine, tgt_engine)
                if not portable:
                    continue
                # Columnas GENERATED (STORED/VIRTUAL) se excluyen: el motor las recalcula
                # solo. Escribirles un valor explicito da un warning (MySQL 1906) que en
                # sql_mode estricto se promueve a error y aborta la tabla completa.
                data_specs.append(_DataSpec(
                    table=t.table,
                    columns=[c.name for c in t.columns if c.computed is None],
                    primary_key=list(t.primary_key),
                    upsert=upsert,
                    row_estimate=None,
                ))

        return _ExecutionPlan(
            clean_statements=clean,
            structure_statements=structure,
            data_specs=data_specs,
            skipped=skipped,
            will_adopt=job.adopt_target and selection is None and job.source_database_id is not None,
            table_order=[t.table for t in self._data_table_order(filtered)],
            warnings=(
                self._autoincrement_pk_warnings(filtered, tgt_engine)
                + self._external_fk_warnings(tgt_adapter, job)
            ),
        )

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

        MySQL/MariaDB inyectan el esquema ORIGEN en las referencias del cuerpo (p. ej.
        ``VIEW_DEFINITION`` siempre trae ``from `origen`.`tabla` ``). Si se clona tal cual,
        el objeto en la BD DESTINO seguiría leyendo de la BD ORIGEN (fuga cross-database y
        clon roto si el origen cambia/desaparece). Reescribimos SOLO el calificador del
        esquema origen → destino (``` `origen`. ``` → ``` `destino`. ```), preservando
        referencias intencionales a OTRAS bases. El backtick de cierre delimita el nombre
        completo, así que no hay coincidencias por prefijo. Solo aplica a la familia
        MySQL/MariaDB (PostgreSQL califica por schema ``public``, igual en origen y destino).
        """
        if source_db == target_db or tgt_engine not in _MYSQL_FAMILY:
            return sql
        return sql.replace(f"`{source_db}`.", f"`{target_db}`.")

    @staticmethod
    def _data_table_order(snap: SchemaSnapshot):
        """Tablas ordenadas topológicamente (padre antes que hijo) para insertar datos."""
        from app.services.db_admin.schema_diff import _table_dep_order
        by_name = {t.table: t for t in snap.tables}
        rank = _table_dep_order(list(by_name), by_name)
        return sorted(snap.tables, key=lambda t: (rank.get(t.table, 0), t.table))

    # ------------------------------------------------------------------ #
    # Token de confirmación                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def clone_execution_token(
        target_ref: str, target_engine: str, plan: _ExecutionPlan, *, clean_mode: str, target_mode: str
    ) -> str:
        """SHA256 del plan EXACTO (modo destino/limpieza + estructura + tablas de datos + adopt)."""
        parts: list[str] = [str(target_ref), str(target_engine), clean_mode, target_mode]
        for s in plan.clean_statements:
            parts.append(f"clean:{s.object_type}:{s.sql}")
        for s in plan.structure_statements:
            parts.append(f"struct:{s.object_type}:{s.sql}")
        for d in plan.data_specs:
            parts.append(f"data:{d.table}:{','.join(d.columns)}:{int(d.upsert)}")
        parts.append(f"adopt:{int(plan.will_adopt)}")
        blob = "\x1f".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Preview                                                             #
    # ------------------------------------------------------------------ #
    def preview(self, job_id: int, selection: list[dict] | None, *, update_selection: bool = False) -> dict:
        """
        Resuelve el plan final SIN ejecutar y devuelve el ``confirm_token``. Si
        ``update_selection`` es True, ``selection`` (incluida None = clon completo) reemplaza
        la del plan y se re-persiste.
        """
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            if update_selection:
                job.selection = json.dumps(selection) if selection is not None else None
                session.commit()
                session.refresh(job)
            src_server = get_server_or_404(session, job.source_server_id)
            tgt_server = get_server_or_404(session, job.target_server_id)
            src_target = build_target(src_server)
            tgt_target = build_target(tgt_server)
            source_snap = get_adapter(src_target).structural_snapshot(job.source_database_name)
            target_snap = None
            if job.clean_mode == CLONE_CLEAN_OBJECTS:
                target_snap = get_adapter(tgt_target).structural_snapshot(job.target_database_name)
            plan = self._build_execution_plan(job, source_snap, target_snap, tgt_target=tgt_target)
            target_ref = f"{job.target_server_id}:{job.target_database_name}"
            target_engine = job.target_engine
            target_managed_id = job.target_database_id
            clean_mode = job.clean_mode
            target_mode = job.target_mode
            target_db_name = job.target_database_name
            token = self.clone_execution_token(
                target_ref, target_engine, plan, clean_mode=clean_mode, target_mode=target_mode
            )
            job.confirm_token = token
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
                {"table": d.table, "row_estimate": d.row_estimate, "upsert": d.upsert}
                for d in plan.data_specs
            ],
            "skipped": plan.skipped,
            "will_adopt": plan.will_adopt,
            "warnings": plan.warnings,
            "confirm_token": token,
        }

    def _assert_not_expired(self, job: CloneJob) -> None:
        if job.expires_at < _utcnow():
            raise AppHttpException(
                message="El plan de clonación expiró; vuelve a crearlo.",
                status_code=410,
                context={"clone_job_id": job.id},
            )

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
            if job.status != CLONE_STATUS_PENDING:
                raise AppHttpException(
                    message=f"El job ya está en estado '{job.status}'; no se puede re-ejecutar.",
                    status_code=409,
                    context={"status": job.status},
                )
            if confirm_target_name != job.target_database_name:
                raise AppHttpException(
                    message="confirm_target_name no coincide con el nombre de la BD destino.",
                    status_code=422,
                    context={},
                )
            # Cuarentena (solo destino gestionado).
            if job.target_database_id is not None and not force:
                md = session.get(ManagedDatabase, job.target_database_id)
                if md is not None and md.status == ProvisionStatus.error:
                    raise AppHttpException(
                        message="El destino está en cuarentena (status=error). Reintenta con force=true.",
                        status_code=409,
                        context={"target_database_id": job.target_database_id},
                    )
            src_server = get_server_or_404(session, job.source_server_id)
            tgt_server = get_server_or_404(session, job.target_server_id)
            src_target = build_target(src_server)
            tgt_target = build_target(tgt_server)
            source_db = job.source_database_name
            target_ref = f"{job.target_server_id}:{job.target_database_name}"
            target_engine = job.target_engine
            clean_mode = job.clean_mode
            target_mode = job.target_mode
            server_id = job.target_server_id
            managed_id = job.target_database_id
        finally:
            session.close()

        # Anti-TOCTOU: re-snapshotear el origen y revalidar el token contra el plan ACTUAL.
        source_snap = get_adapter(src_target).structural_snapshot(source_db)
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            if _snapshot_fingerprint(source_snap) != job.source_fingerprint:
                raise AppHttpException(
                    message="El esquema del origen cambió desde que se creó el plan; vuelve a crearlo.",
                    status_code=409,
                    context={"clone_job_id": job_id},
                )
            target_snap = None
            if clean_mode == CLONE_CLEAN_OBJECTS:
                target_snap = get_adapter(tgt_target).structural_snapshot(job.target_database_name)
            plan = self._build_execution_plan(job, source_snap, target_snap, tgt_target=tgt_target)
            expected = self.clone_execution_token(
                target_ref, target_engine, plan, clean_mode=clean_mode, target_mode=target_mode
            )
            if confirm_token != expected:
                raise AppHttpException(
                    message="confirm_token no coincide con el plan actual; vuelve a previsualizar.",
                    status_code=422,
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
                f"clon {job_id} → {target_ref} "
                f"(clean={clean_mode}, mode={target_mode}, data={bool(plan.data_specs)})"
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
                "include_data": job.include_data,
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

        # Anti-TOCTOU final (el origen pudo cambiar entre execute y run).
        source_snap = src_adapter.structural_snapshot(ctx["source_db"])
        if _snapshot_fingerprint(source_snap) != ctx["source_fp"]:
            self._set_status(job_id, CLONE_STATUS_FAILED, finished=True,
                             error="El esquema del origen cambió antes de ejecutar; replanea.")
            return

        target_snap = None
        if ctx["clean_mode"] == CLONE_CLEAN_OBJECTS:
            target_snap = tgt_adapter.structural_snapshot(ctx["target_db"])

        # Reconstruir el plan en el worker (fuente de verdad final).
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            plan = self._build_execution_plan(job, source_snap, target_snap, tgt_target=tgt_target)
        finally:
            session.close()

        # Todas las fases MUTANTES (limpiar → estructura → datos → adopt) corren bajo UN
        # ÚNICO advisory lock del motor, sostenido durante todo el pipeline en una conexión
        # dedicada del worker. Así se serializan cross-proceso dos clones al mismo destino
        # (o un clon vs. un execute de schema-comparison sobre la misma BD física) — el
        # lock abarca también DROP/CREATE DATABASE y la fase de datos, no solo el DDL.
        with runner.advisory_lock(tgt_target, engine=engine, lock_key=lock_key):
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
        if ctx["target_mode"] == CLONE_TARGET_NEW:
            tgt_adapter.create_database(ctx["target_db"])
            self._record_items(job_id, [dict(seq=seq, kind=CLONE_ITEM_CLEAN, object_type="database",
                                             object_name=ctx["target_db"], status=CLONE_ITEM_APPLIED,
                                             executed_at=_utcnow())])
            seq += 1
        elif ctx["clean_mode"] == CLONE_CLEAN_DROP_DATABASE:
            tgt_adapter.drop_database(ctx["target_db"])
            tgt_adapter.create_database(ctx["target_db"])
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
        if not had_failure and ctx["include_data"] and plan.data_specs:
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
                TableCopySpec(table=d.table, columns=d.columns, primary_key=d.primary_key, upsert=d.upsert)
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
                item_rows.append(dict(seq=seq, kind=CLONE_ITEM_DATA, object_type="table",
                                      object_name=res.table, status=status, error=error,
                                      rows_copied=res.rows_copied, executed_at=_utcnow()))
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
                f"clon {job_id} → {ctx['target_ref']} "
                f"(clean={ctx['clean_mode']}, mode={ctx['target_mode']}, data={ctx['include_data']})"
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
        results = runner.execute_adhoc(
            tgt_target, db_name=db_name, engine=engine, lock_key=lock_key, statements=sqls,
            already_locked=True, disable_fk_checks=disable_fk_checks,
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
                already_locked=True, stop_on_error=False,
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
        from app.controllers.managed_database_controller import ManagedDatabaseController

        session = self._session()
        try:
            src_md = session.get(ManagedDatabase, ctx["src_managed_id"]) if ctx["src_managed_id"] else None
            if src_md is None or src_md.model_id is None:
                return  # el origen ya no es gestionado con blueprint; nada que adoptar
            model_id = src_md.model_id
            model_version = src_md.model_version
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
            },
            admin=None,
        )
