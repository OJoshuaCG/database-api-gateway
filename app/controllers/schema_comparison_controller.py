"""
Controller de comparaciones estructurales entre dos BDs gestionadas (Plan diff).

Responsabilidades (Fases 4-6):
- **Crear** una comparación: snapshotea ambas BDs (motor), corre el diff PURO,
  renderiza el DDL para el motor del TARGET y persiste cabecera + ítems +
  fingerprints. Solo lectura del motor.
- **Leer** el resumen y los ítems paginados (dry-run/preview obligatorio: nunca se
  ejecuta nada sin haber mostrado el DDL exacto primero).
- **Adoptar** (Opción A): ensambla el DDL seleccionado como una NUEVA versión del
  blueprint del target (reusa ``ModelMigrationController.create_migration``) y,
  opcionalmente, la aplica por el camino normal (``ManagedMigrationController.apply``).
- **Ejecutar** (Opción B): corre el DDL seleccionado directamente sobre el target
  (``MigrationRunner.execute_adhoc``). BLOQUEADO si el target tiene blueprint.

Seguridad transversal:
- El servidor es la ÚNICA fuente de verdad del SQL (nunca se ejecuta SQL que
  reenvíe el cliente); el cliente solo confirma con ``confirm_target_name`` +
  ``confirm_token`` (hash del conjunto EXACTO a ejecutar, recomputado server-side).
- Anti-TOCTOU: antes de adoptar/ejecutar se re-snapshotea el target y se recompara
  el fingerprint (409, sin ``force``).
- Auditoría fail-closed (``record_intent``) ANTES de tocar el motor en toda
  ejecución (Opción A inmediata y Opción B).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    SCHEMA_COMPARISON_MAX_ITEMS,
    SCHEMA_COMPARISON_MAX_SQL_BYTES,
    SCHEMA_COMPARISON_TTL_HOURS,
)
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.models.enums import EngineType, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.schema_comparison import SchemaComparison
from app.models.schema_comparison_item import SchemaComparisonItem
from app.services import audit
from app.services.db_admin.export_spec import sanitize_filename
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import references_gateway_internal_table
from app.services.db_admin.migrations import MigrationRunner
from app.services.db_admin.plan_integrity import (
    PlanItem,
    blocking,
    check_closure,
    expand_selection,
    prune_unsatisfied,
    validate_statement_plan,
)
from app.services.db_admin.schema_diff import diff_snapshots
from app.services.db_admin.sql_dialect import (
    BODY_OBJECT_TYPES,
    body_delimiter_wrapper,
    requalify_body_schema,
)

# Motores de la misma familia SQL (comparables entre sí). PostgreSQL solo consigo mismo.
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
# Tipos de objeto cuyo cuerpo/DDL NO es traducible cross-engine (atan el blueprint al
# motor de origen vía el guard cross-engine existente).
_NON_PORTABLE_TYPES = frozenset(
    {"routine", "view", "materialized_view", "trigger", "event"}
)


def _utcnow() -> datetime:
    """UTC naive (consistente con el almacenamiento DateTime sin tz en los 3 motores)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot_fingerprint(snapshot) -> str:
    """
    Hash estable del snapshot NORMALIZADO (anti-TOCTOU). Excluye lo cosmético:
    ``captured_at`` y la versión de extensión (no son estructura). Cualquier cambio
    estructural (una tabla/columna/índice) altera el hash y dispara el 409.
    """
    payload = snapshot.model_dump(mode="json")
    payload.pop("captured_at", None)
    for ext in payload.get("extensions") or []:
        ext.pop("version", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _ResolvedSide:
    """
    Un lado (source|target) ya resuelto a datos planos, desacoplado de la sesión ORM.

    ``managed_id`` es el ``managed_database_id`` si la BD está en el inventario, o
    ``None`` si es una BD cruda no registrada. ``target`` (``ServerTarget``) lleva la
    credencial pseudo-root descifrada en memoria — se usa el tiempo mínimo para abrir la
    conexión y nunca se serializa/loguea.
    """

    server_id: int
    database_name: str
    engine: str
    target: ServerTarget
    managed_id: int | None
    model_id: int | None
    quarantined: bool
    # True solo para una referencia CRUDA que no existe (aún) en el inventario: hay que
    # validar su existencia real en el motor en vivo antes de snapshotear.
    needs_live_check: bool


def _synthetic_lock_key(server_id: int, database_name: str) -> int:
    """
    Clave de advisory lock SINTÉTICA para una BD SIN gestionar (sin managed_database_id).

    Propiedades garantizadas (críticas para ``pg_try_advisory_lock``, que interpola el
    valor DIRECTO en el SQL):
    - **Siempre negativa** → nunca colisiona con un ``managed_database_id`` real
      (autoincrement, siempre positivo): un id real y una BD sin gestionar nunca comparten
      lock por accidente.
    - **Determinística** para el mismo ``(server_id, database_name)`` → dos ejecuciones
      concurrentes sobre la MISMA BD física sin gestionar SÍ se serializan entre sí.
    - **bigint firmado válido**: la magnitud se acota a 62 bits, así que el resultado cae
      en ``[-2**62, -1]`` (holgado dentro de ``[-2**63, 2**63-1]``).

    El valor lo produce SIEMPRE este código a partir de un hash — nunca es texto de
    usuario — así que interpolarlo en el SQL del lock es seguro.
    """
    digest = hashlib.sha256(f"{server_id}:{database_name}".encode("utf-8")).digest()
    magnitude = int.from_bytes(digest[:8], "big") & ((1 << 62) - 1)
    return -(magnitude + 1)  # +1 garantiza estrictamente negativo (nunca 0)


class SchemaComparisonController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Helpers de carga                                                    #
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

    def _resolve_side(
        self,
        session,
        *,
        database_id: int | None,
        server_id: int | None,
        database_name: str | None,
    ) -> _ResolvedSide:
        """
        Resuelve un lado (source|target) a ``_ResolvedSide`` DENTRO de la sesión (la
        credencial se descifra mientras la sesión sigue abierta). Se asume que el schema
        Pydantic ya garantizó EXACTAMENTE una representación (id, o server_id+name).

        - Por ``database_id``: ``ManagedDatabase`` por id (404 si no existe) → gestionada.
        - Por ``(server_id + database_name)``: se resuelve el ``Server`` (404) y se
          AUTO-RESUELVE contra el inventario: si ya existe un ``ManagedDatabase`` para ese
          par exacto (garantizado único por ``UniqueConstraint(server_id, name)``), se
          trata IDÉNTICO a haber pasado su id (mismo ``managed_id`` → mismo lock, misma
          cuarentena, Opción A disponible). Si no existe, queda "sin gestionar"
          (``managed_id=None``) y se marca ``needs_live_check`` para validar su existencia
          real en el motor antes de snapshotear.
        """
        if database_id is not None:
            md = self._db_or_404(session, database_id)
            server = get_server_or_404(session, md.server_id)
            return _ResolvedSide(
                server_id=md.server_id,
                database_name=md.name,
                engine=engine_value(server),
                target=build_target(server),
                managed_id=md.id,
                model_id=md.model_id,
                quarantined=md.status == ProvisionStatus.error,
                needs_live_check=False,
            )

        # Referencia cruda: (server_id + database_name).
        server = get_server_or_404(session, server_id)
        md = (
            session.query(ManagedDatabase)
            .filter(
                ManagedDatabase.server_id == server_id,
                ManagedDatabase.name == database_name,
            )
            .one_or_none()
        )
        if md is not None:
            # Auto-resolución: la BD cruda YA está en el inventario → trátala como el id.
            return _ResolvedSide(
                server_id=server_id,
                database_name=md.name,
                engine=engine_value(server),
                target=build_target(server),
                managed_id=md.id,
                model_id=md.model_id,
                quarantined=md.status == ProvisionStatus.error,
                needs_live_check=False,
            )
        return _ResolvedSide(
            server_id=server_id,
            database_name=database_name,
            engine=engine_value(server),
            target=build_target(server),
            managed_id=None,
            model_id=None,
            quarantined=False,
            needs_live_check=True,
        )

    @staticmethod
    def _assert_live_exists(side: _ResolvedSide) -> None:
        """
        Para una referencia CRUDA no registrada: valida que la BD exista de verdad en el
        motor en vivo (404 explícito y accionable, en vez de un error opaco del driver al
        intentar snapshotear una BD inexistente). No-op si la BD ya está en el inventario.
        """
        if not side.needs_live_check:
            return
        if side.database_name not in get_adapter(side.target).list_databases():
            raise AppHttpException(
                message=(
                    f"La base de datos '{side.database_name}' no existe en el servidor "
                    f"{side.server_id} (o es una BD del sistema, no gestionable)."
                ),
                status_code=404,
                context={"server_id": side.server_id, "database": side.database_name},
            )

    @staticmethod
    def _audit_target_kwargs(managed_id: int | None) -> dict:
        """
        kwargs de auditoría (``target_type``/``target_id``) del target: si está en el
        inventario → ``managed_database`` + su id; si es una BD cruda sin registro →
        ``server_database`` + ``target_id=None`` (no hay FK que lo represente; el
        servidor + nombre van en el ``detail``).
        """
        if managed_id is not None:
            return {"target_type": "managed_database", "target_id": managed_id}
        return {"target_type": "server_database", "target_id": None}

    @staticmethod
    def _comparison_or_404(session, comparison_id: int) -> SchemaComparison:
        comp = session.get(SchemaComparison, comparison_id)
        if comp is None:
            raise AppHttpException(
                message="Comparación de esquema no encontrada.",
                status_code=404,
                context={"comparison_id": comparison_id},
            )
        return comp

    @staticmethod
    def _assert_not_expired(comp: SchemaComparison) -> None:
        if comp.expires_at is not None and comp.expires_at < _utcnow():
            raise AppHttpException(
                message=(
                    "La comparación expiró; describe un estado del motor que ya no es "
                    "vigente. Recalcúlala (POST /schema-comparisons)."
                ),
                status_code=410,
                context={"comparison_id": comp.id, "expires_at": str(comp.expires_at)},
            )

    @staticmethod
    def _engines_compatible(a: str, b: str) -> bool:
        if a == b:
            return True
        return a in _MYSQL_FAMILY and b in _MYSQL_FAMILY

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _counts(items) -> dict[str, dict[str, int]]:
        """
        Conteo por (object_type -> change_type -> nº de OBJETOS distintos).

        Se cuenta por ``object_name`` distinto (no por sentencia): un objeto que
        rinde N sentencias sigue contando como 1 cambio.
        """
        seen: dict[tuple[str, str], set[str]] = {}
        for it in items:
            seen.setdefault((it.object_type, it.change_type), set()).add(it.object_name)
        out: dict[str, dict[str, int]] = {}
        for (ot, ct), names in seen.items():
            out.setdefault(ot, {})[ct] = len(names)
        return out

    def _serialize_summary(
        self, comp: SchemaComparison, *, items
    ) -> dict:
        has_destructive = any(
            json.loads(it.risk_flags).get("destructive") for it in items
        )
        return {
            "id": comp.id,
            "source_server_id": comp.source_server_id,
            "source_database_name": comp.source_database_name,
            "target_server_id": comp.target_server_id,
            "target_database_name": comp.target_database_name,
            "source_database_id": comp.source_database_id,
            "target_database_id": comp.target_database_id,
            "source_engine": comp.source_engine,
            "target_engine": comp.target_engine,
            "cross_flavor_warning": comp.cross_flavor_warning,
            "scope_note": comp.scope_note,
            "item_count": len(items),
            "counts": self._counts(items),
            "has_destructive": has_destructive,
            "expired": bool(comp.expires_at is not None and comp.expires_at < _utcnow()),
            "created_at": comp.created_at,
            "expires_at": comp.expires_at,
        }

    @staticmethod
    def _serialize_item(it: SchemaComparisonItem) -> dict:
        return {
            "id": it.id,
            "comparison_id": it.comparison_id,
            "seq": it.seq,
            "object_type": it.object_type,
            "object_name": it.object_name,
            "change_type": it.change_type,
            "phase": it.phase,
            "sql": it.sql,
            "risk_flags": json.loads(it.risk_flags),
            "down_sql": it.down_sql,
            "down_confirmed": it.down_confirmed,
            # Atomicidad + dependencias: el frontend los necesita para no dejar marcar
            # media redefinición ni una vista sin su tabla (y para explicar el porqué).
            "op_group": it.op_group,
            "depends_on": (
                json.loads(it.depends_on) if it.depends_on else []
            ),
            "execution_status": it.execution_status,
            "execution_error": it.execution_error,
            "executed_at": it.executed_at,
        }

    # ------------------------------------------------------------------ #
    # Fase 4 — Crear comparación                                          #
    # ------------------------------------------------------------------ #
    def create_comparison(
        self,
        *,
        source_database_id: int | None = None,
        source_server_id: int | None = None,
        source_database_name: str | None = None,
        target_database_id: int | None = None,
        target_server_id: int | None = None,
        target_database_name: str | None = None,
        admin: dict | None = None,
    ) -> dict:
        # 1) Resolver ambos lados DENTRO de la sesión (la credencial se descifra mientras
        #    la sesión sigue abierta). Cada lado acepta id de inventario o (server+nombre)
        #    crudo; una referencia cruda a una BD ya registrada se auto-resuelve a su id.
        session = self._session()
        try:
            src = self._resolve_side(
                session,
                database_id=source_database_id,
                server_id=source_server_id,
                database_name=source_database_name,
            )
            tgt = self._resolve_side(
                session,
                database_id=target_database_id,
                server_id=target_server_id,
                database_name=target_database_name,
            )
        finally:
            session.close()

        # 2) Identidad física: source y target no pueden ser la MISMA BD. Se compara por
        #    (server_id, nombre) — así cubre id==id, id vs cruda y cruda vs cruda que
        #    apunten a la misma BD real (tras la auto-resolución quedan equivalentes).
        if src.server_id == tgt.server_id and src.database_name == tgt.database_name:
            raise AppHttpException(
                message="source y target no pueden ser la misma base de datos.",
                status_code=422,
                context={"server_id": tgt.server_id, "database": tgt.database_name},
            )

        # 3) Compatibilidad de motor (antes de tocar el motor: fail temprano y barato).
        if not self._engines_compatible(src.engine, tgt.engine):
            raise AppHttpException(
                message=(
                    f"Motores incompatibles: no se puede comparar '{src.engine}' con "
                    f"'{tgt.engine}'. Se permite MySQL↔MariaDB; PostgreSQL solo con PostgreSQL."
                ),
                status_code=422,
                context={"source_engine": src.engine, "target_engine": tgt.engine},
            )

        # 4) Existencia real en el motor de las BDs crudas no registradas (404 explícito
        #    antes de snapshotear). No-op para las que ya están en el inventario.
        self._assert_live_exists(src)
        self._assert_live_exists(tgt)

        # 5) Snapshots (motor, solo lectura) + diff PURO + render para el TARGET.
        source_snap = get_adapter(src.target).structural_snapshot(src.database_name)
        target_snap = get_adapter(tgt.target).structural_snapshot(tgt.database_name)
        diff = diff_snapshots(source_snap, target_snap)
        rendered = get_adapter(tgt.target).render_diff(diff)

        # Re-calificación de esquema en cuerpos (vistas/rutinas/triggers/eventos): MySQL/
        # MariaDB persisten el esquema ORIGEN calificado DENTRO del cuerpo (una vista trae
        # ``from `origen`.`tabla` ``). Sin reescribir origen→destino, el SQL renderizado
        # referenciaría la BD ORIGEN cuando luego se adopta como versión de blueprint, se
        # ejecuta ad-hoc (Opción B) o se exporta → tabla inexistente en el destino o fuga
        # cross-database. Se hace ACÁ, una sola vez, porque todos los consumidores
        # (adopt/execute/export) leen el ``sql`` ya persistido. Mismo tratamiento que el
        # clonado (``CloneController._requalify_body``). No-op si ambos lados comparten
        # nombre de BD o el motor no es de la familia MySQL.
        if src.database_name != tgt.database_name and tgt.engine in _MYSQL_FAMILY:
            for r in rendered:
                if r.object_type in BODY_OBJECT_TYPES:
                    r.sql = requalify_body_schema(
                        r.sql, src.database_name, tgt.database_name, tgt.engine
                    )
                    if r.down_sql:
                        r.down_sql = requalify_body_schema(
                            r.down_sql, src.database_name, tgt.database_name, tgt.engine
                        )

        # 3) Guardrails de tamaño (fail temprano; no materializar payloads enormes).
        if len(rendered) > SCHEMA_COMPARISON_MAX_ITEMS:
            raise AppHttpException(
                message=(
                    f"La comparación produjo {len(rendered)} sentencias (máx. "
                    f"{SCHEMA_COMPARISON_MAX_ITEMS}). ¿Son BDs realmente comparables?"
                ),
                status_code=422,
                context={"items": len(rendered), "max": SCHEMA_COMPARISON_MAX_ITEMS},
            )
        total_bytes = sum(len(r.sql.encode("utf-8")) for r in rendered)
        if total_bytes > SCHEMA_COMPARISON_MAX_SQL_BYTES:
            raise AppHttpException(
                message=(
                    f"El DDL total ({total_bytes} bytes) supera el máximo "
                    f"({SCHEMA_COMPARISON_MAX_SQL_BYTES})."
                ),
                status_code=422,
                context={"bytes": total_bytes, "max": SCHEMA_COMPARISON_MAX_SQL_BYTES},
            )

        # 6) Persistir cabecera + ítems + fingerprints. La BD física de cada lado
        #    (server_id + nombre) se guarda SIEMPRE; el managed_database_id solo si esa
        #    BD está en el inventario (NULL si es cruda).
        src_fp = _snapshot_fingerprint(source_snap)
        tgt_fp = _snapshot_fingerprint(target_snap)
        expires = _utcnow() + timedelta(hours=SCHEMA_COMPARISON_TTL_HOURS)
        session = self._session()
        try:
            comp = SchemaComparison(
                source_server_id=src.server_id,
                source_database_name=src.database_name,
                target_server_id=tgt.server_id,
                target_database_name=tgt.database_name,
                source_database_id=src.managed_id,
                target_database_id=tgt.managed_id,
                source_engine=src.engine,
                target_engine=tgt.engine,
                source_fingerprint=src_fp,
                target_fingerprint=tgt_fp,
                cross_flavor_warning=diff.cross_flavor_warning,
                scope_note=diff.scope_note,
                expires_at=expires,
            )
            session.add(comp)
            session.flush()  # asigna comp.id
            for i, r in enumerate(rendered):
                session.add(
                    SchemaComparisonItem(
                        comparison_id=comp.id,
                        seq=i,
                        object_type=r.object_type,
                        object_name=r.object_name,
                        change_type=r.change_type,
                        phase=r.phase,
                        sql=r.sql,
                        risk_flags=json.dumps(r.risk.model_dump(), sort_keys=True),
                        down_sql=r.down_sql,
                        down_confirmed=r.down_confirmed,
                        # Grupo atómico + grafo de dependencias: se persisten con el ítem
                        # porque adopt/execute validan la selección MUCHO después, sin los
                        # snapshots en memoria (recalcularlos exigiría re-snapshotear ambas
                        # BDs y volver a diffear).
                        op_group=r.op_group or None,
                        depends_on=json.dumps(r.depends_on) if r.depends_on else None,
                    )
                )
            session.commit()
            session.refresh(comp)
            items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comp.id)
                .all()
            )
            comp_id = comp.id
            result = self._serialize_summary(comp, items=items)
        finally:
            session.close()

        audit.record(
            "schema_comparison.create",
            admin=admin,
            **self._audit_target_kwargs(tgt.managed_id),
            server_id=tgt.server_id,
            touched_engine=True,  # se snapshotearon ambos motores (solo lectura)
            detail=(
                f"comparación {comp_id}: source={src.server_id}/{src.database_name} vs "
                f"target={tgt.server_id}/{tgt.database_name}, {len(rendered)} sentencia(s)"
            ),
        )
        return result

    # ------------------------------------------------------------------ #
    # Fase 4 — Lectura                                                    #
    # ------------------------------------------------------------------ #
    def get_comparison(self, comparison_id: int) -> dict:
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .all()
            )
            return self._serialize_summary(comp, items=items)
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Export — descarga del diff como archivo .sql                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """
        Deja solo alfanuméricos, ``.-_`` (resto → ``_``) para un filename seguro.

        El criterio vive en ``export_spec.sanitize_filename`` (lo comparte con el módulo
        de exportación, que construye nombres desde una plantilla): dos saneadores de
        nombre de archivo distintos serían dos superficies distintas de recorrido de ruta.
        """
        return sanitize_filename(name, fallback="db")

    def _render_statement_block(self, it: dict, engine: str) -> str:
        """
        Un bloque SQL por ítem: comentario de contexto + la sentencia terminada en ``;``.

        Para objetos con cuerpo procedural en la familia MySQL, envuelve con
        ``DELIMITER $$ ... $$ DELIMITER ;`` para que el cuerpo con ``;`` internos sea
        ejecutable por un cliente estándar (el motor del gateway ejecuta por sentencia
        única, así que este envoltorio es solo para el archivo que descarga el usuario).
        """
        destructive = " [DESTRUCTIVO]" if it["risk"].get("destructive") else ""
        review = (
            " [REQUIERE REVISIÓN INDIVIDUAL]"
            if it["risk"].get("requires_individual_review")
            else ""
        )
        header = (
            f"-- [{it['seq']}] {it['object_type']} {it['object_name']} "
            f"({it['change_type']}){destructive}{review}"
        )
        sql = it["sql"].rstrip().rstrip(";")
        # El criterio de la envoltura vive en ``sql_dialect`` (fuente única): lo comparte
        # con el writer de exportación vía ``ServerAdapter.export_body_wrapper``. Tenerlo
        # duplicado acá era garantía de que un día un archivo saliera ejecutable y el otro
        # roto con la misma rutina adentro.
        wrapper = body_delimiter_wrapper(it["object_type"], engine)
        if wrapper is not None:
            prefix, suffix = wrapper
            body = f"{prefix}{sql}{suffix}"
        else:
            body = f"{sql};"
        return f"{header}\n{body}"

    def export_sql(
        self,
        comparison_id: int,
        *,
        item_ids: list[int] | None = None,
        object_type: str | None = None,
        change_type: str | None = None,
        include_rollback: bool = False,
        admin: dict | None = None,
    ) -> tuple[str, str]:
        """
        Ensambla el DDL del diff como un único texto ``.sql`` descargable y devuelve
        ``(filename, content)``.

        Selección (todas combinables; el resultado son los ítems que cumplen TODOS los
        filtros, ordenados por ``seq``):
        - ``item_ids``: exactamente estos ítems (validando pertenencia); ``None`` = todos.
        - ``object_type`` / ``change_type``: filtros como en ``list_items``.

        Solo LEE ítems ya persistidos (no toca ningún motor, no valida fingerprint): es un
        artefacto de revisión, no una ejecución. Funciona idéntico para BDs adoptadas y
        crudas (el recurso ya está keyed por comparación). ``include_rollback`` anexa el
        ``down_sql`` sugerido (orden inverso) COMENTADO al final.
        """
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            q = session.query(SchemaComparisonItem).filter(
                SchemaComparisonItem.comparison_id == comparison_id
            )
            if object_type is not None:
                q = q.filter(SchemaComparisonItem.object_type == object_type)
            if change_type is not None:
                q = q.filter(SchemaComparisonItem.change_type == change_type)
            total_in_comparison = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .count()
            )
            rows = q.order_by(SchemaComparisonItem.seq.asc()).all()
            if item_ids is not None:
                idset = set(item_ids)
                found = {r.id for r in rows}
                missing = [i for i in item_ids if i not in found]
                if missing:
                    raise AppHttpException(
                        message=(
                            "Algunos ítems seleccionados no pertenecen a esta comparación "
                            "(o no pasan los filtros indicados)."
                        ),
                        status_code=422,
                        context={
                            "comparison_id": comparison_id,
                            "missing_item_ids": missing,
                        },
                    )
                rows = [r for r in rows if r.id in idset]
            # Datos planos ANTES de cerrar la sesión.
            selected = [
                {
                    "id": r.id,
                    "seq": r.seq,
                    "sql": r.sql,
                    "down_sql": r.down_sql,
                    "object_type": r.object_type,
                    "object_name": r.object_name,
                    "change_type": r.change_type,
                    "risk": json.loads(r.risk_flags),
                }
                for r in rows
            ]
            meta = {
                "id": comp.id,
                "source_server_id": comp.source_server_id,
                "source_database_name": comp.source_database_name,
                "target_server_id": comp.target_server_id,
                "target_database_name": comp.target_database_name,
                "source_engine": comp.source_engine,
                "target_engine": comp.target_engine,
                "created_at": comp.created_at,
                "expires_at": comp.expires_at,
                "target_managed_id": comp.target_database_id,
            }
        finally:
            session.close()

        if not selected:
            raise AppHttpException(
                message="No hay sentencias para exportar con la selección/filtros indicados.",
                status_code=422,
                context={"comparison_id": comparison_id},
            )

        target_engine = meta["target_engine"]
        expired = bool(meta["expires_at"] is not None and meta["expires_at"] < _utcnow())
        expired_note = "  [EXPIRADA — puede no reflejar el estado actual]" if expired else ""
        lines = [
            "-- ==========================================================================",
            f"-- Schema diff export — comparación #{meta['id']}",
            f"-- Generado: {_utcnow().isoformat(timespec='seconds')} UTC",
            f"-- Snapshot de la comparación: {meta['created_at']}{expired_note}",
            (
                f"-- Source (deseado): servidor {meta['source_server_id']} / "
                f"{meta['source_database_name']} ({meta['source_engine']})"
            ),
            (
                f"-- Target (a modificar): servidor {meta['target_server_id']} / "
                f"{meta['target_database_name']} ({target_engine})"
            ),
            (
                f"-- Sentencias exportadas: {len(selected)} de {total_in_comparison} "
                "en la comparación"
            ),
            "-- ADVERTENCIA: DDL derivado de un snapshot; el gateway NO garantiza que el",
            "--   esquema siga vigente. Revísalo antes de ejecutarlo contra el target.",
            "-- ==========================================================================",
            "",
        ]
        for it in selected:
            lines.append(self._render_statement_block(it, target_engine))
            lines.append("")

        if include_rollback:
            down_parts = [d for d in (it["down_sql"] for it in reversed(selected)) if d]
            lines.append(
                "-- ======================================================================"
            )
            if down_parts:
                lines.append(
                    "-- ROLLBACK sugerido (orden inverso) — COMENTADO. Revísalo antes de usar."
                )
                lines.append(
                    "-- ===================================================================="
                )
                for part in down_parts:
                    for ln in part.rstrip().rstrip(";").splitlines():
                        lines.append(f"-- {ln}")
                    lines.append("-- ;")
            else:
                lines.append(
                    "-- ROLLBACK: no hay down_sql sugerido para los ítems exportados."
                )
                lines.append(
                    "-- ===================================================================="
                )
            lines.append("")

        content = "\n".join(lines)
        filename = (
            f"schema-diff-{meta['id']}-"
            f"{self._sanitize_filename(meta['target_database_name'])}.sql"
        )

        audit.record(
            "schema_comparison.export",
            admin=admin,
            **self._audit_target_kwargs(meta["target_managed_id"]),
            server_id=meta["target_server_id"],
            touched_engine=False,
            detail=(
                f"export .sql de la comparación {meta['id']}: {len(selected)} de "
                f"{total_in_comparison} sentencia(s)"
                + (" (con rollback)" if include_rollback else "")
            ),
        )
        return filename, content

    def list_items(
        self,
        comparison_id: int,
        *,
        object_type: str | None = None,
        change_type: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._comparison_or_404(session, comparison_id)
            q = session.query(SchemaComparisonItem).filter(
                SchemaComparisonItem.comparison_id == comparison_id
            )
            if object_type is not None:
                q = q.filter(SchemaComparisonItem.object_type == object_type)
            if change_type is not None:
                q = q.filter(SchemaComparisonItem.change_type == change_type)
            total = q.count()
            rows = (
                q.order_by(SchemaComparisonItem.seq.asc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize_item(r) for r in rows], total
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Integridad del plan: grupos atómicos y cierre de dependencias        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _plan_items(rows: list) -> list[PlanItem]:
        """
        Adapta filas de ``schema_comparison_items`` a los DTOs puros de
        ``plan_integrity``. Las comparaciones creadas ANTES de que existieran
        ``op_group``/``depends_on`` tienen ambos en NULL: se degrada a un grupo por
        sentencia y sin dependencias (comportamiento anterior), nunca se inventa un
        grafo que no se calculó.
        """
        out: list[PlanItem] = []
        for r in rows:
            try:
                deps = tuple(json.loads(r.depends_on)) if r.depends_on else ()
            except (TypeError, ValueError):
                deps = ()
            try:
                risk = json.loads(r.risk_flags) if r.risk_flags else {}
            except (TypeError, ValueError):
                risk = {}
            out.append(
                PlanItem(
                    id=r.id,
                    seq=r.seq,
                    op_group=r.op_group or f"{r.object_type}|{r.object_name}|{r.change_type}|{r.id}",
                    depends_on=deps,
                    object_type=r.object_type,
                    object_name=r.object_name,
                    change_type=r.change_type,
                    has_down_sql=bool(r.down_sql),
                    destructive=bool(risk.get("destructive")),
                )
            )
        return out

    def _assert_selection_closed(
        self, plan: list[PlanItem], selected_item_ids: list[int], *, operation: str
    ) -> None:
        """
        Rechaza (422) una selección EXPLÍCITA a la que le faltan dependencias o que parte
        un grupo atómico.

        Es el guard que faltaba y que explica una clase entera de fallos en producción: el
        gateway aceptaba cualquier subconjunto de ítems y armaba el ``up_sql`` con él, así
        que "adoptar la vista pero no la tabla que lee" o "adoptar el ``ADD`` de un índice
        redefinido sin su ``DROP``" solo se descubría cuando el motor abortaba la
        migración a mitad de camino. La respuesta incluye los ids que faltan para que el
        frontend pueda ofrecer "agregarlos" (o llamar a ``/resolve-selection``).
        """
        missing = check_closure(plan, selected_item_ids)
        if not missing:
            return
        closure = expand_selection(plan, selected_item_ids)
        raise AppHttpException(
            message=(
                f"La selección no es ejecutable: {len(missing)} cambio(s) dependen de "
                "otros que no fueron seleccionados (o quedó partido un cambio que se "
                "ejecuta como DROP+CREATE). Agregue los ítems faltantes o use "
                "/resolve-selection para completarlos automáticamente."
            ),
            status_code=422,
            context={"operation": operation, "missing_dependencies": missing},
            public_context={
                # El frontend NECESITA esto para guiar al admin, no es solo debug.
                "missing_dependencies": missing,
                "suggested_item_ids": list(closure.item_ids),
                "would_add_item_ids": list(closure.added_item_ids),
            },
        )

    @staticmethod
    def _assert_no_gateway_internal_sql(resolved: list[dict], *, operation: str) -> None:
        """
        Rechaza (409) ejecutar/adoptar sentencias que toquen la contabilidad INTERNA del
        gateway (``_gw_v_{slug}`` / ``_gw_stg_*``).

        La causa raíz ya está corregida: ``structural_snapshot`` excluye esas tablas, así
        que una comparación NUEVA no puede generar ese DDL. Esto cubre las comparaciones
        **YA PERSISTIDAS antes del fix**, que siguen vivas hasta que expire su TTL
        (``SCHEMA_COMPARISON_TTL_HOURS``) con el SQL malo guardado en
        ``schema_comparison_items``.

        Hace falta acá y no alcanza con los guards de migraciones porque la **Opción B**
        (``/execute``) NO pasa por ``create_migration``: ejecuta el SQL persistido directo
        con ``MigrationRunner.execute_adhoc``. Los dos sentidos son peligrosos: si el
        target tenía la tabla de versión, el diff emitió ``DROP TABLE _gw_v_…``; si la
        tenía el SOURCE, emitió ``CREATE TABLE _gw_v_{slug_del_origen}``, que le inyecta al
        target una tabla de versión ajena (y en un clon previo al fix podía llegar incluso
        con su fila, haciendo que el gateway creyera que esa BD está en una versión que no
        tiene).

        Es 409 y no 422 porque el cliente no puede corregirlo cambiando el input: los
        ítems son de solo lectura. La salida es **recalcular la comparación** — misma
        semántica y mismo CTA que el 409 anti-TOCTOU.
        """
        offenders: dict[int, list[str]] = {}
        for d in resolved:
            hits: list[str] = []
            for sql in (d.get("sql"), d.get("down_sql")):
                for prefix in references_gateway_internal_table(sql or ""):
                    if prefix not in hits:
                        hits.append(prefix)
            if hits:
                offenders[d["id"]] = hits
        if not offenders:
            return
        raise AppHttpException(
            message=(
                "Esta comparación fue calculada antes de una corrección del sistema y "
                "contiene sentencias contra las tablas internas del gateway "
                f"({', '.join(sorted({p for v in offenders.values() for p in v}))}*), que "
                "administran el puntero de versión de las migraciones. Ejecutarlas dejaría "
                "la base de datos sin versión registrada. Recalculá la comparación "
                "(POST /schema-comparisons) y volvé a intentar: el diff nuevo ya no las "
                "incluye."
            ),
            status_code=409,
            context={"operation": operation, "offending_item_ids": sorted(offenders)},
            public_context={
                "offending_item_ids": sorted(offenders),
                "recalculate_required": True,
            },
        )

    def _assert_plan_sane(
        self, plan: list[PlanItem], selected_item_ids: list[int], *, operation: str
    ) -> list:
        """
        Corre el linter sobre el plan YA cerrado y aborta (422) ante cualquier hallazgo
        bloqueante. Devuelve los hallazgos informativos (avisos) para exponerlos.

        Barrera de último recurso: verifica el INVARIANTE de que toda dependencia se
        ejecuta antes que su dependiente. Si el ordenador de ``schema_diff`` regresara,
        esto falla acá —en el gateway, sin tocar el motor— en vez de escribir una versión
        de blueprint que reventará en cada BD donde se aplique.
        """
        chosen = set(selected_item_ids)
        findings = validate_statement_plan([p for p in plan if p.id in chosen])
        hard = blocking(findings)
        if hard:
            raise AppHttpException(
                message=(
                    "El plan de sentencias no pasó la validación de integridad: "
                    + "; ".join(f.message for f in hard[:3])
                ),
                status_code=422,
                context={"operation": operation},
                public_context={
                    "plan_errors": [
                        {"code": f.code, "message": f.message, "op_group": f.op_group}
                        for f in hard
                    ]
                },
            )
        return [
            {"code": f.code, "message": f.message, "op_group": f.op_group}
            for f in findings
        ]

    def resolve_selection(self, comparison_id: int, selected_item_ids: list[int]) -> dict:
        """
        Expande una selección a su CIERRE de dependencias sin ejecutar ni adoptar nada.

        Mismo patrón que ``POST /database-clones/{id}/resolve-selection`` del clonado: el
        frontend manda lo que el usuario marcó y recibe el conjunto realmente ejecutable,
        con el detalle de qué se agregó y por qué. Evita el ciclo "intento → 422 → agrego
        → reintento".
        """
        session = self._session()
        try:
            self._comparison_or_404(session, comparison_id)
            rows = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .order_by(SchemaComparisonItem.seq.asc())
                .all()
            )
            plan = self._plan_items(rows)
            by_id = {r.id: r for r in rows}
        finally:
            session.close()

        ids = list(dict.fromkeys(selected_item_ids))
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise AppHttpException(
                message="Algunos ítems seleccionados no pertenecen a esta comparación.",
                status_code=422,
                context={"comparison_id": comparison_id, "missing_item_ids": unknown},
            )
        closure = expand_selection(plan, ids)
        reasons = check_closure(plan, ids)
        return {
            "comparison_id": comparison_id,
            "requested_item_ids": ids,
            "resolved_item_ids": list(closure.item_ids),
            "added_item_ids": list(closure.added_item_ids),
            "added_reasons": reasons,
            "added": [
                {
                    "item_id": by_id[i].id,
                    "object_type": by_id[i].object_type,
                    "object_name": by_id[i].object_name,
                    "change_type": by_id[i].change_type,
                    "sql": by_id[i].sql,
                }
                for i in closure.added_item_ids
            ],
            "total": len(closure.item_ids),
        }

    # ------------------------------------------------------------------ #
    # Anti-TOCTOU                                                         #
    # ------------------------------------------------------------------ #
    def _assert_fingerprint(
        self, server_id: int, database_name: str, stored_fp: str, *, label: str
    ) -> None:
        """
        Re-snapshotea la BD física (``server_id`` + ``database_name``) y compara el
        fingerprint contra el guardado (409 si difiere). Keying por servidor+nombre —
        no por ``managed_database_id`` — para funcionar igual con BDs gestionadas y crudas.
        """
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            target = build_target(server)
        finally:
            session.close()

        current_fp = _snapshot_fingerprint(
            get_adapter(target).structural_snapshot(database_name)
        )
        if current_fp != stored_fp:
            raise AppHttpException(
                message=(
                    f"El esquema de {label} cambió desde que se calculó esta comparación; "
                    "recalcúlala (POST /schema-comparisons) antes de continuar."
                ),
                status_code=409,
                context={"server_id": server_id, "database": database_name, "side": label},
            )

    def _verify_no_drift(self, comp: SchemaComparison, *, side: str = "target") -> None:
        """Recompara fingerprints del target (y opcionalmente el source) — anti-TOCTOU."""
        self._assert_fingerprint(
            comp.target_server_id, comp.target_database_name, comp.target_fingerprint,
            label="target",
        )
        if side == "both":
            self._assert_fingerprint(
                comp.source_server_id, comp.source_database_name, comp.source_fingerprint,
                label="source",
            )

    # ------------------------------------------------------------------ #
    # Fase 5 — Opción A: adoptar como versión de blueprint                #
    # ------------------------------------------------------------------ #
    def adopt_comparison(
        self,
        comparison_id: int,
        *,
        selected_item_ids: list[int],
        name: str,
        description: str | None = None,
        execute_immediately: bool = False,
        auto_resolve_dependencies: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            self._assert_not_expired(comp)
            # Re-resolver el estado ACTUAL del target por (server_id, nombre): la Opción A
            # solo existe si el target está en el inventario Y tiene blueprint. Se re-resuelve
            # (no se confía en el managed_database_id persistido, que pudo quedar obsoleto).
            tgt_md = (
                session.query(ManagedDatabase)
                .filter(
                    ManagedDatabase.server_id == comp.target_server_id,
                    ManagedDatabase.name == comp.target_database_name,
                )
                .one_or_none()
            )
            if tgt_md is None:
                raise AppHttpException(
                    message=(
                        "El target no está en el inventario del gateway (es una BD cruda no "
                        "registrada): la adopción como versión de blueprint (Opción A) no está "
                        "disponible. Usa /execute (Opción B)."
                    ),
                    status_code=422,
                    context={
                        "server_id": comp.target_server_id,
                        "database": comp.target_database_name,
                    },
                )
            if tgt_md.model_id is None:
                raise AppHttpException(
                    message=(
                        "El target no tiene blueprint asignado (model_id): la adopción como "
                        "versión de blueprint (Opción A) no está disponible. Usa /execute."
                    ),
                    status_code=422,
                    context={"target_database_id": tgt_md.id},
                )
            model_id = tgt_md.model_id
            target_db_id = tgt_md.id
            target_server_id = tgt_md.server_id
            target_db_name = tgt_md.name
            target_engine = comp.target_engine
            target_fp = comp.target_fingerprint
            # TODOS los ítems: hacen falta para validar el cierre de dependencias de la
            # selección (una dependencia no seleccionada igual tiene que ser conocida).
            all_rows = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .order_by(SchemaComparisonItem.seq.asc())
                .all()
            )
            # Extraer los ítems seleccionados YA como datos planos (la sesión se cierra).
            selected = self._load_selected(session, comparison_id, selected_item_ids)
            # DTOs puros (no filas ORM): la sesión se cierra a continuación.
            plan = self._plan_items(all_rows)
        finally:
            session.close()

        # --- Integridad de la selección ANTES de crear la versión ------------- #
        # Cerrar (o rechazar) la selección: adoptar un subconjunto incoherente escribe una
        # versión de blueprint que fallará en TODAS las BDs del modelo, no solo en esta.
        effective_ids = [d["id"] for d in selected]
        added_ids: list[int] = []
        if auto_resolve_dependencies:
            closure = expand_selection(plan, effective_ids)
            added_ids = list(closure.added_item_ids)
            if added_ids:
                effective_ids = list(closure.item_ids)
                session = self._session()
                try:
                    selected = self._load_selected(session, comparison_id, effective_ids)
                finally:
                    session.close()
        # El cierre se re-verifica SIEMPRE, incluso después de expandirlo: si la expansión
        # tuviera un hueco, el linter no lo detectaría (ignora dependencias ausentes del
        # subconjunto, porque para entonces ya se asume cerrado).
        self._assert_selection_closed(plan, effective_ids, operation="adopt")
        self._assert_no_gateway_internal_sql(selected, operation="adopt")
        plan_warnings = self._assert_plan_sane(plan, effective_ids, operation="adopt")

        # Anti-TOCTOU antes de derivar/aplicar nada.
        self._assert_fingerprint(target_server_id, target_db_name, target_fp, label="target")

        # Ensamblar up_sql (orden de EJECUCIÓN) y down_sql (orden inverso).
        selected.sort(key=lambda d: d["seq"])
        up_sql = ";\n".join(d["sql"] for d in selected)
        down_parts = [d["down_sql"] for d in reversed(selected) if d["down_sql"]]
        down_suggested = ";\n".join(down_parts) if down_parts else None
        # Auto-confirmar el rollback SOLO si TODO el conjunto es claramente reversible
        # (cada ítem con down_sql confirmado). Si no, queda como sugerencia (fail-closed).
        all_confirmed = bool(selected) and all(
            d["down_confirmed"] and d["down_sql"] for d in selected
        )
        down_confirmed = down_suggested if all_confirmed else None
        has_non_portable = any(d["object_type"] in _NON_PORTABLE_TYPES for d in selected)

        # Pin del SQL al motor del target (evita que el traductor cross-engine lo mangle:
        # es DDL específico de dialecto). Igual que el baseline de snapshot (Plan 09).
        up_mysql = up_sql if target_engine in _MYSQL_FAMILY else None
        up_pg = up_sql if target_engine == "postgresql" else None

        # Auditoría fail-closed ANTES de tocar el motor SOLO si se aplica de inmediato
        # (crear la versión no toca ningún motor; aplicarla sí).
        if execute_immediately:
            audit.record_intent(
                "schema_comparison.adopt",
                admin=admin,
                target_type="managed_database",
                target_id=target_db_id,
                server_id=target_server_id,
                detail=(
                    f"adopt+apply de {len(selected)} sentencia(s) desde comparación "
                    f"{comparison_id} al blueprint {model_id}"
                ),
            )

        # REUSO: create_migration resuelve checksum, autoasignación de versión con
        # reintento por concurrencia y bump de current_version en una transacción.
        # is_baseline=True + reviewed según execute_immediately: DDL derivado del motor
        # es "capturado" (como un baseline de snapshot). Si se difiere la aplicación,
        # nace reviewed=False y el gate R1 lo protege hasta que un admin lo apruebe;
        # si se aplica de inmediato, la selección explícita del admin ES la revisión.
        from app.controllers.model_migration_controller import ModelMigrationController

        migration = ModelMigrationController().create_migration(
            model_id,
            {
                "name": name,
                "up_sql": up_sql,
                "up_sql_mysql": up_mysql,
                "up_sql_postgresql": up_pg,
                "down_sql": down_confirmed,
                "down_sql_suggested": down_suggested,
                "source_engine": target_engine,
                "has_non_portable": has_non_portable,
                "kind": "schema",
                "is_baseline": True,
                "reviewed": execute_immediately,
                # MANIFIESTO de sentencias: una fila por sentencia con su reverso
                # EMPAREJADO y su ``seq`` alineado al índice del checkpoint. Es lo que
                # habilita reconciliar una aplicación parcial (deshacer exactamente las
                # sentencias que commitearon) en vez de quedar con la BD a mitad de camino.
                "statements": [
                    {
                        "up_sql": d["sql"],
                        "down_sql": d["down_sql"],
                        "down_confirmed": d["down_confirmed"],
                        "object_type": d["object_type"],
                        "object_name": d["object_name"],
                        "op_group": d["op_group"],
                        "destructive": bool(d["risk"].get("destructive")),
                    }
                    for d in selected
                ],
            },
            admin=admin,
        )
        version = migration["version"]

        apply_result = None
        if execute_immediately:
            from app.controllers.managed_migration_controller import (
                ManagedMigrationController,
            )

            # Camino normal, con TODOS sus guards (integridad, cross-engine, cuarentena,
            # R1 — que aquí pasa porque reviewed=True). up_to_version acota a la versión
            # recién creada (no re-aplica versiones anteriores del blueprint más allá de
            # lo pendiente hasta ella).
            apply_result = ManagedMigrationController().apply(
                target_db_id, up_to_version=version, admin=admin
            )

        audit.record(
            "schema_comparison.adopt",
            status="success",
            admin=admin,
            target_type="managed_database",
            target_id=target_db_id,
            server_id=target_server_id,
            touched_engine=execute_immediately,
            detail=(
                f"versión {version} creada en blueprint {model_id} desde comparación "
                f"{comparison_id} ({len(selected)} sentencia(s))"
                + (" y aplicada" if execute_immediately else "")
            ),
        )
        return {
            "comparison_id": comparison_id,
            "model_id": model_id,
            "version": version,
            "statements": len(selected),
            "executed": execute_immediately,
            "migration": migration,
            "apply_result": apply_result,
            # Ítems que hubo que agregar para cerrar dependencias (solo con
            # auto_resolve_dependencies=true) y avisos NO bloqueantes del linter de plan.
            "added_item_ids": added_ids,
            "plan_warnings": plan_warnings,
        }

    @staticmethod
    def _load_selected(
        session, comparison_id: int, selected_item_ids: list[int]
    ) -> list[dict]:
        """Carga los ítems seleccionados validando pertenencia; devuelve datos planos."""
        ids = list(dict.fromkeys(selected_item_ids))  # dedup preservando orden
        rows = (
            session.query(SchemaComparisonItem)
            .filter(
                SchemaComparisonItem.comparison_id == comparison_id,
                SchemaComparisonItem.id.in_(ids),
            )
            .all()
        )
        found = {r.id for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise AppHttpException(
                message="Algunos ítems seleccionados no pertenecen a esta comparación.",
                status_code=422,
                context={"comparison_id": comparison_id, "missing_item_ids": missing},
            )
        return [
            {
                "id": r.id,
                "seq": r.seq,
                "sql": r.sql,
                "down_sql": r.down_sql,
                "down_confirmed": r.down_confirmed,
                "object_type": r.object_type,
                "object_name": r.object_name,
                "change_type": r.change_type,
                "op_group": r.op_group
                or f"{r.object_type}|{r.object_name}|{r.change_type}|{r.id}",
                "risk": json.loads(r.risk_flags),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Fase 6 — Opción B: ejecución directa ad-hoc                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def execution_token(target_ref: str, target_engine: str, resolved: list[dict]) -> str:
        """
        Token de confirmación verificable: SHA256 del conjunto EXACTO a ejecutar
        (``target_ref`` + ``engine`` + lista ORDENADA de ``(sql, risk_flags)``).

        ``target_ref`` liga el token a la BD física del target de forma UNIFORME para
        BDs gestionadas y crudas: ``f"{target_server_id}:{target_database_name}"`` (ambos
        SIEMPRE poblados), en vez del ``managed_database_id`` (que ahora puede ser NULL).

        Es un checksum de integridad, no un secreto: liga la confirmación del cliente
        al SQL que realmente vio. Si la selección cambia (o alguien reordena), el token
        no coincide. Se recomputa server-side y solo se usa para COMPARAR; nunca se
        confía en el valor del cliente para otra cosa. El cliente obtiene el token de
        ``/execute-preview`` (no lo recalcula) — este cambio es transparente para él.
        """
        parts: list[str] = [str(target_ref), str(target_engine)]
        for d in resolved:
            parts.append(d["sql"])
            parts.append(json.dumps(d["risk"], sort_keys=True))
        blob = "\x1f".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _resolve_mode(
        self, all_items: list, mode: str, selected_item_ids: list[int] | None
    ) -> tuple[list[dict], list[str], list[dict]]:
        """
        Resuelve el conjunto de sentencias a ejecutar según el modo (ya ordenadas por seq)
        y lo VALIDA. Devuelve ``(sentencias, grupos_excluidos_por_dependencia, avisos)``.

        - ``all``: todo lo aditivo/seguro y destructivo, EXCEPTO objetos procedurales
          con cuerpo no revisable (``requires_individual_review``: solo vía custom).
        - ``all_except_destructive``: excluye además cualquier ítem ``destructive``.
        - ``custom``: exactamente los ``selected_item_ids`` (el admin los eligió a mano),
          pero se VALIDA que la selección cierre sobre sus dependencias.

        **Los modos automáticos se PODAN, no fallan.** El filtro por riesgo es por
        sentencia y puede dejar fuera una dependencia sin sacar a sus dependientes. Caso
        real: una tabla nueva que la heurística marcó ``possible_rename_of`` es
        ``destructive`` y queda fuera de ``all_except_destructive``, pero sus índices y
        FKs (no destructivos) NO — y un ``CREATE INDEX`` sobre una tabla que no existe
        aborta la ejecución. Se descartan transitivamente los cambios que quedaron sin
        base y se reporta cuáles (el admin ve exactamente qué NO se va a ejecutar).
        """
        def to_dict(it) -> dict:
            return {
                "id": it.id,
                "seq": it.seq,
                "sql": it.sql,
                "object_type": it.object_type,
                "object_name": it.object_name,
                "risk": json.loads(it.risk_flags),
            }

        plan = self._plan_items(all_items)
        dicts = [to_dict(it) for it in all_items]  # ya vienen ordenados por seq
        by_id = {d["id"]: d for d in dicts}

        if mode in ("all", "all_except_destructive"):
            keep = [
                d["id"]
                for d in dicts
                if not d["risk"].get("requires_individual_review")
                and not (mode == "all_except_destructive" and d["risk"].get("destructive"))
            ]
            kept_ids, pruned = prune_unsatisfied(plan, keep)
            resolved = [by_id[i] for i in kept_ids]
        elif mode == "custom":
            if not selected_item_ids:
                raise AppHttpException(
                    message="mode=custom requiere 'selected_item_ids'.",
                    status_code=422,
                    context={"mode": mode},
                )
            idset = set(selected_item_ids)
            missing = idset - set(by_id)
            if missing:
                raise AppHttpException(
                    message="Algunos 'selected_item_ids' no pertenecen a esta comparación.",
                    status_code=422,
                    context={"missing_item_ids": sorted(missing)},
                )
            self._assert_selection_closed(plan, sorted(idset), operation="execute")
            resolved, pruned = [d for d in dicts if d["id"] in idset], []
        else:
            raise AppHttpException(
                message="Modo de ejecución inválido.",
                status_code=422,
                context={"mode": mode},
            )

        # Comparaciones calculadas ANTES del fix del snapshot pueden traer DDL contra la
        # contabilidad interna del gateway. La Opción B ejecuta el SQL persistido directo
        # (no pasa por create_migration), así que el guard tiene que estar acá.
        self._assert_no_gateway_internal_sql(resolved, operation=f"execute:{mode}")

        # Linter de plan: última barrera antes de tocar el motor. Un plan cuyo orden viole
        # una dependencia no se ejecuta (fallaría a mitad, dejando la BD inconsistente).
        warnings = self._assert_plan_sane(
            plan, [d["id"] for d in resolved], operation=f"execute:{mode}"
        )
        return resolved, pruned, warnings

    def preview_execution(
        self,
        comparison_id: int,
        *,
        mode: str,
        selected_item_ids: list[int] | None,
    ) -> dict:
        """
        Resuelve el conjunto de sentencias de un modo/selección de Opción B SIN
        ejecutar nada, y devuelve el ``confirm_token`` correspondiente.

        Existe porque el frontend NO puede reproducir de forma confiable ``_resolve_mode``
        (requeriría paginar TODOS los ítems y replicar el filtro por ``risk_flags``) ni el
        formato EXACTO de serialización de ``execution_token`` (orden de claves JSON,
        separadores) — ambos son detalles de implementación del servidor. El servidor sigue
        siendo la única fuente de verdad del token: esta es la única vía soportada para
        obtenerlo antes de llamar a ``/execute``.
        """
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            self._assert_not_expired(comp)
            # Ref estable (server+nombre, siempre poblado) para el token; el
            # managed_database_id es solo informativo para el frontend (puede ser NULL).
            target_ref = f"{comp.target_server_id}:{comp.target_database_name}"
            target_managed_id = comp.target_database_id
            target_engine = comp.target_engine
            all_items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .order_by(SchemaComparisonItem.seq.asc())
                .all()
            )
            resolved, pruned, plan_warnings = self._resolve_mode(all_items, mode, selected_item_ids)
        finally:
            session.close()

        if not resolved:
            raise AppHttpException(
                message="No hay sentencias que ejecutar para el modo/selección indicados.",
                status_code=422,
                context={"comparison_id": comparison_id, "mode": mode},
            )

        token = self.execution_token(target_ref, target_engine, resolved)
        return {
            "comparison_id": comparison_id,
            "target_database_id": target_managed_id,
            "mode": mode,
            "statements": [
                {
                    "item_id": d["id"],
                    "object_type": d["object_type"],
                    "object_name": d["object_name"],
                    "sql": d["sql"],
                    "risk_flags": d["risk"],
                }
                for d in resolved
            ],
            # Cambios descartados porque su dependencia quedó fuera del modo automático
            # (p. ej. un índice de una tabla excluida por destructiva). Visible en el
            # preview para que el admin no descubra la ausencia después de ejecutar.
            "excluded_by_dependency": pruned,
            "plan_warnings": plan_warnings,
            "confirm_token": token,
        }

    def execute_comparison(
        self,
        comparison_id: int,
        *,
        mode: str,
        selected_item_ids: list[int] | None,
        confirm_target_name: str,
        confirm_token: str,
        force: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            self._assert_not_expired(comp)
            target_server_id = comp.target_server_id
            db_name = comp.target_database_name
            target_engine = comp.target_engine
            target_fp = comp.target_fingerprint
            # Ref estable (server+nombre) para el token: uniforme entre BD gestionada y cruda.
            target_ref = f"{target_server_id}:{db_name}"
            # Re-resolver el estado gestionado ACTUAL del target por (server_id, nombre). No
            # se confía en el managed_database_id persistido: si la BD se adoptó (y le
            # asignaron blueprint) DESPUÉS de crear la comparación, hay que detectarlo aquí
            # (el fingerprint no lo capta: adoptar no cambia la estructura). Consistente con
            # el comportamiento previo, que releía el ManagedDatabase fresco en cada execute.
            tgt_md = (
                session.query(ManagedDatabase)
                .filter(
                    ManagedDatabase.server_id == target_server_id,
                    ManagedDatabase.name == db_name,
                )
                .one_or_none()
            )
            # Decisión #3: la ejecución directa está BLOQUEADA si el target tiene blueprint
            # (dejaría la BD desincronizada de su propio blueprint sin que el sistema se entere).
            if tgt_md is not None and tgt_md.model_id is not None:
                raise AppHttpException(
                    message=(
                        "El target tiene un blueprint asignado: la ejecución directa (Opción B) "
                        "está bloqueada. Usa /adopt (Opción A) para agregar una versión al blueprint."
                    ),
                    status_code=409,
                    context={"target_database_id": tgt_md.id, "model_id": tgt_md.model_id},
                )
            # Confirmación por nombre (doble intención, human-meaningful) contra el nombre
            # persistido de la BD física (siempre poblado, gestionada o cruda).
            if confirm_target_name != db_name:
                raise AppHttpException(
                    message=(
                        "Confirmación requerida: 'confirm_target_name' debe coincidir "
                        "exactamente con el nombre de la BD target."
                    ),
                    status_code=422,
                    context={"database": db_name, "required": "confirm_target_name == name"},
                )
            # managed_id/cuarentena/lock_key según si la BD está o no en el inventario. Sin
            # gestionar → no hay concepto de cuarentena (no hay fila que auditar) y el lock
            # es la clave sintética negativa por (server_id, nombre).
            managed_id = tgt_md.id if tgt_md is not None else None
            quarantined = tgt_md is not None and tgt_md.status == ProvisionStatus.error
            lock_key = (
                managed_id
                if managed_id is not None
                else _synthetic_lock_key(target_server_id, db_name)
            )
            server = get_server_or_404(session, target_server_id)
            engine = EngineType(engine_value(server))
            target = build_target(server)
            all_items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .order_by(SchemaComparisonItem.seq.asc())
                .all()
            )
            resolved, pruned, plan_warnings = self._resolve_mode(all_items, mode, selected_item_ids)
        finally:
            session.close()

        if not resolved:
            raise AppHttpException(
                message="No hay sentencias que ejecutar para el modo/selección indicados.",
                status_code=422,
                context={"comparison_id": comparison_id, "mode": mode},
            )

        # Confirmación verificable (hash), recomputada server-side sobre el conjunto EXACTO.
        expected = self.execution_token(target_ref, target_engine, resolved)
        if confirm_token != expected:
            raise AppHttpException(
                message=(
                    "'confirm_token' no coincide con el conjunto de sentencias a ejecutar "
                    "(¿cambió la selección desde que se calculó?). Reobtén el token."
                ),
                status_code=422,
                context={"comparison_id": comparison_id, "mode": mode},
            )

        # Cuarentena: un fallo previo pudo dejar la BD en estado parcial (DDL no
        # transaccional). Se exige inspección + force=true (igual que apply). Solo aplica
        # a BDs gestionadas: una BD cruda no tiene fila de estado, "nunca en cuarentena".
        if quarantined and not force:
            raise AppHttpException(
                message=(
                    "La BD está en cuarentena por un fallo previo. Inspecciónala y "
                    "reintenta con force=true."
                ),
                status_code=409,
                context={"target_database_id": managed_id, "required": "force=true"},
            )

        # Anti-TOCTOU JUSTO antes de ejecutar (por servidor+nombre; gestionada o cruda).
        self._assert_fingerprint(target_server_id, db_name, target_fp, label="target")

        managed_note = "gestionada" if managed_id is not None else "sin gestionar"
        # Auditoría fail-closed ANTES de tocar el motor (persiste la intención completa).
        audit.record_intent(
            "schema_comparison.execute",
            admin=admin,
            **self._audit_target_kwargs(managed_id),
            server_id=target_server_id,
            detail=(
                f"execute mode={mode}: {len(resolved)} sentencia(s) de la comparación "
                f"{comparison_id} sobre servidor {target_server_id}/'{db_name}' "
                f"({managed_note}, sin blueprint)"
            ),
        )

        statements = [d["sql"] for d in resolved]
        results = MigrationRunner().execute_adhoc(
            target,
            db_name=db_name,
            engine=engine,
            lock_key=lock_key,
            statements=statements,
        )
        self._record_item_results(resolved, results)

        failed = any(r.status == "failed" for r in results)
        applied_count = sum(1 for r in results if r.status == "applied")
        audit.record(
            "schema_comparison.execute",
            status="error" if failed else "success",
            admin=admin,
            **self._audit_target_kwargs(managed_id),
            server_id=target_server_id,
            touched_engine=True,
            detail=(
                f"{applied_count}/{len(resolved)} sentencia(s) aplicada(s) sobre "
                f"servidor {target_server_id}/'{db_name}' ({managed_note})"
                + (" (con fallo)" if failed else "")
            ),
        )
        return {
            "comparison_id": comparison_id,
            "target_database_id": managed_id,
            "mode": mode,
            "total": len(resolved),
            "applied_count": applied_count,
            "failed": failed,
            "statements": self._result_items(resolved, results),
            "excluded_by_dependency": pruned,
            "plan_warnings": plan_warnings,
        }

    @staticmethod
    def _result_items(resolved: list[dict], results: list) -> list[dict]:
        out: list[dict] = []
        for k, d in enumerate(resolved):
            if k < len(results):
                r = results[k]
                out.append(
                    {
                        "item_id": d["id"],
                        "object_type": d["object_type"],
                        "object_name": d["object_name"],
                        "status": r.status,
                        "error": r.error,
                        "execution_ms": r.execution_ms,
                    }
                )
            else:
                # Sentencia no ejecutada (se cortó en un fallo anterior).
                out.append(
                    {
                        "item_id": d["id"],
                        "object_type": d["object_type"],
                        "object_name": d["object_name"],
                        "status": "skipped",
                        "error": None,
                        "execution_ms": None,
                    }
                )
        return out

    def _record_item_results(self, resolved: list[dict], results: list) -> None:
        """Persiste el resultado por sentencia en ``schema_comparison_items``."""
        session = self._session()
        try:
            for k, d in enumerate(resolved):
                item = session.get(SchemaComparisonItem, d["id"])
                if item is None:
                    continue
                if k < len(results):
                    r = results[k]
                    item.execution_status = r.status
                    item.execution_error = r.error
                    item.executed_at = r.executed_at
                else:
                    item.execution_status = "skipped"
                    item.execution_error = None
                    item.executed_at = None
            session.commit()
        finally:
            session.close()
